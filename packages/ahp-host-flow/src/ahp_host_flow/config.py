"""`flow.toml`: providers, routing and workflow sources (plan §5)."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from ahp_host_flow.engine.redact import Redactor

# The names VS Code recognises; copied from ahp-host-claude, which a provider package cannot be
# imported sideways for.
APPROVAL_KEY: Final = "permissionMode"
EFFORT_KEY: Final = "effort"
APPROVAL_MODES: Final = ("default", "acceptEdits", "auto", "plan")
EFFORT_LEVELS: Final = ("low", "medium", "high", "xhigh", "max")
SOURCES: Final = ("workspace", "user", "host")
# A .py workflow is arbitrary code on the host: only these sources supply one (plan §4.10).
PYTHON_SOURCES_DEFAULT: Final = ("user", "host")
DEFAULT_STATE_DIR: Final = Path("~/.local/state/ahp-host-flow")
PROVIDER_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
LOAD_SPEC: Final = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_][\w.]*")
REMOTE_URL: Final = re.compile(r"wss?://\S+")


class ConfigError(ValueError):
    """The config file is missing, unparseable or invalid."""


@dataclass(frozen=True)
class RoutingRule:
    provider: str
    model: str | None = None
    phase: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class Routing:
    default: str
    rules: tuple[RoutingRule, ...] = ()


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    # 'module:Class', imported and constructed on first use, not at startup. None for a fleet or
    # remote provider.
    load: str | None
    # Handed to the provider: a path to its own config file, or a table.
    config: Mapping[str, Any] | Path | None = None
    # Alias -> model id.
    models: Mapping[str, str] = field(default_factory=dict)
    # Passed verbatim to the inner session.
    approval_mode: str | None = None
    effort: str | None = None
    # Fleet: `via = "fleet"` runs the call on a gateway node (`node` is the gateway's node id).
    via: str | None = None
    node: str | None = None
    # Fleet and remote: the agent id to start there.
    agent: str | None = None
    # Remote: dial another host directly.
    url: str | None = None
    token_file: Path | None = None
    # Fleet: seconds a fleet tool may take to answer before the call is retried once on the
    # rejoined gateway, then failed. None: `fleet.REPLY_TIMEOUT`.
    reply_timeout: float | None = None
    # Remote: seconds the remote turn may go without an event (a question or an approval
    # nobody can answer from here) before the call fails. None: `remote.IDLE_TIMEOUT`.
    idle_timeout: float | None = None

    @property
    def kind(self) -> Literal["inprocess", "fleet", "remote"]:
        if self.load is not None:
            return "inprocess"
        return "fleet" if self.via is not None else "remote"


@dataclass(frozen=True)
class WorkflowsConfig:
    dirs: tuple[Path, ...] = ()
    sources: tuple[str, ...] = SOURCES
    python_sources: tuple[str, ...] = PYTHON_SOURCES_DEFAULT


@dataclass(frozen=True)
class RedactConfig:
    # Extra regexes, applied after the builtin credential shapes.
    patterns: tuple[str, ...] = ()
    builtin: bool = True


@dataclass(frozen=True)
class FlowConfig:
    # In TOML order.
    providers: Mapping[str, ProviderConfig]
    routing: Routing
    state_dir: Path
    node: str | None = None
    workflows: WorkflowsConfig = WorkflowsConfig()
    redact: RedactConfig = RedactConfig()


def _table(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a table")
    return value


def _check_keys(table: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _str(table: Mapping[str, Any], key: str, where: str) -> str | None:
    value = table.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be a string")
    return value


def _seconds(table: Mapping[str, Any], key: str, where: str) -> float | None:
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError(f"{where}.{key} must be a positive number of seconds")
    return float(value)


def _path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _str_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be an array of strings")
    return value


def _parse_provider(name: str, raw: Any, base: Path) -> ProviderConfig:
    where = f"providers.{name}"
    if not PROVIDER_NAME.fullmatch(name):
        raise ConfigError(f"provider name {name!r} must match {PROVIDER_NAME.pattern}")
    table = _table(raw, where)
    present = [key for key in ("load", "via", "url") if key in table]
    if not present:
        raise ConfigError(f"{where} needs one of load, via, url")
    if len(present) > 1:
        raise ConfigError(f"{where}: load, via and url are exclusive")
    if "load" in table:
        kind = "inprocess"
        allowed: tuple[str, ...] = ("load", "config", "models", "approval_mode", "effort")
    elif "via" in table:
        kind = "fleet"
        allowed = ("via", "node", "agent", "models", "approval_mode", "effort", "reply_timeout")
    else:
        kind = "remote"
        allowed = ("url", "token_file", "agent", "models", "idle_timeout")
    _check_keys(table, allowed, where)
    load = via = node = agent = url = None
    token_file: Path | None = None
    config: Mapping[str, Any] | Path | None = None
    if kind == "inprocess":
        load = _str(table, "load", where)
        if load is None or not LOAD_SPEC.fullmatch(load):
            raise ConfigError(f"{where}.load must be 'module:Class', got {load!r}")
        raw_config = table.get("config")
        if isinstance(raw_config, str):
            config = _path(raw_config, base)
        elif isinstance(raw_config, Mapping):
            config = dict(raw_config)
        elif raw_config is not None:
            raise ConfigError(f"{where}.config must be a path or a table")
    elif kind == "fleet":
        via = _str(table, "via", where)
        if via != "fleet":
            raise ConfigError(f"{where}.via must be 'fleet', got {via!r}")
        node = _str(table, "node", where)
        if not node:
            raise ConfigError(f"{where}.node is required")
    else:
        url = _str(table, "url", where)
        if url is None or not REMOTE_URL.fullmatch(url):
            raise ConfigError(f"{where}.url must be a ws:// or wss:// URL")
        raw_token = _str(table, "token_file", where)
        if raw_token is not None:
            token_file = _path(raw_token, base)
    if kind != "inprocess":
        agent = _str(table, "agent", where)
        if not agent:
            raise ConfigError(f"{where}.agent is required")
    models: dict[str, str] = {}
    for alias, model_id in _table(table.get("models", {}), f"{where}.models").items():
        if not isinstance(model_id, str):
            raise ConfigError(f"{where}.models.{alias} must be a string")
        models[alias] = model_id
    approval_mode = effort = None
    if kind != "remote":
        approval_mode = _str(table, "approval_mode", where)
        if approval_mode is not None and not approval_mode:
            raise ConfigError(f"{where}.approval_mode must not be empty")
        effort = _str(table, "effort", where)
        if effort is not None and effort not in EFFORT_LEVELS:
            raise ConfigError(
                f"{where}.effort must be one of {', '.join(EFFORT_LEVELS)}, got {effort!r}"
            )
    return ProviderConfig(
        name,
        load,
        config,
        models,
        approval_mode,
        effort,
        via,
        node,
        agent,
        url,
        token_file,
        reply_timeout=_seconds(table, "reply_timeout", where),
        idle_timeout=_seconds(table, "idle_timeout", where),
    )


def _parse_routing(raw: Any, providers: Mapping[str, ProviderConfig]) -> Routing:
    table = _table(raw, "routing")
    _check_keys(table, ("default", "rules"), "routing")
    default = _str(table, "default", "routing")
    if default is None:
        if len(providers) != 1:
            raise ConfigError("routing.default is required with more than one provider")
        default = next(iter(providers))
    elif default not in providers:
        raise ConfigError(f"routing.default names unknown provider {default!r}")
    raw_rules = table.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigError("routing.rules must be an array of tables")
    rules: list[RoutingRule] = []
    for i, raw_rule in enumerate(raw_rules):
        where = f"routing.rules[{i}]"
        rule = _table(raw_rule, where)
        _check_keys(rule, ("provider", "model", "phase", "label"), where)
        provider = _str(rule, "provider", where)
        if provider is None:
            raise ConfigError(f"{where}.provider is required")
        if provider not in providers:
            raise ConfigError(f"{where}.provider names unknown provider {provider!r}")
        model = _str(rule, "model", where)
        phase = _str(rule, "phase", where)
        label = _str(rule, "label", where)
        if model is None and phase is None and label is None:
            raise ConfigError(f"{where} needs at least one of model, phase, label")
        rules.append(RoutingRule(provider, model, phase, label))
    return Routing(default, tuple(rules))


def _parse_workflows(raw: Any, base: Path) -> WorkflowsConfig:
    table = _table(raw, "workflows")
    _check_keys(table, ("dirs", "sources", "python_sources"), "workflows")
    dirs = tuple(_path(d, base) for d in _str_list(table.get("dirs", []), "workflows.dirs"))
    sources = tuple(_str_list(table.get("sources", list(SOURCES)), "workflows.sources"))
    for source in sources:
        if source not in SOURCES:
            raise ConfigError(f"workflows.sources: {source!r} is not one of {', '.join(SOURCES)}")
    if len(set(sources)) != len(sources):
        raise ConfigError("workflows.sources has duplicates")
    python_sources = tuple(
        _str_list(
            table.get("python_sources", list(PYTHON_SOURCES_DEFAULT)), "workflows.python_sources"
        )
    )
    for source in python_sources:
        if source not in SOURCES:
            raise ConfigError(
                f"workflows.python_sources: {source!r} is not one of {', '.join(SOURCES)}"
            )
    if len(set(python_sources)) != len(python_sources):
        raise ConfigError("workflows.python_sources has duplicates")
    return WorkflowsConfig(dirs, sources, python_sources)


def _parse_redact(raw: Any) -> RedactConfig:
    table = _table(raw, "redact")
    _check_keys(table, ("patterns", "builtin"), "redact")
    patterns = tuple(_str_list(table.get("patterns", []), "redact.patterns"))
    for i, pattern in enumerate(patterns):
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"redact.patterns[{i}]: {exc}") from exc
        if compiled.search("") is not None:
            raise ConfigError(f"redact.patterns[{i}] matches the empty string")
    builtin = table.get("builtin", True)
    if not isinstance(builtin, bool):
        raise ConfigError("redact.builtin must be true or false")
    return RedactConfig(patterns, builtin)


def make_redactor(redact: RedactConfig) -> Redactor:
    return Redactor(redact.patterns, builtin=redact.builtin)


def parse_config(data: Mapping[str, Any], *, base: Path) -> FlowConfig:
    """Validate `data` (a parsed flow.toml); relative paths resolve against `base`."""
    _check_keys(
        data, ("state_dir", "node", "providers", "routing", "workflows", "redact"), "flow.toml"
    )
    state_dir_raw = _str(data, "state_dir", "flow.toml")
    state_dir = (
        DEFAULT_STATE_DIR.expanduser() if state_dir_raw is None else _path(state_dir_raw, base)
    )
    node = _str(data, "node", "flow.toml")
    raw_providers = _table(data.get("providers", {}), "providers")
    if not raw_providers:
        raise ConfigError("providers needs at least one entry")
    providers = {name: _parse_provider(name, raw, base) for name, raw in raw_providers.items()}
    routing = _parse_routing(data.get("routing", {}), providers)
    workflows = _parse_workflows(data.get("workflows", {}), base)
    redact = _parse_redact(data.get("redact", {}))
    return FlowConfig(providers, routing, state_dir, node, workflows, redact)


def load_config(path: Path) -> FlowConfig:
    path = path.expanduser()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_config(data, base=path.parent)
