"""`flow.toml`: providers, routing and workflow sources (plan §5)."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ahp_host_flow.engine.redact import Redactor

# The names VS Code recognises; copied from ahp-host-claude, which a provider package cannot be
# imported sideways for.
APPROVAL_KEY: Final = "permissionMode"
EFFORT_KEY: Final = "effort"
APPROVAL_MODES: Final = ("default", "acceptEdits", "auto", "plan")
EFFORT_LEVELS: Final = ("low", "medium", "high", "xhigh", "max")
SOURCES: Final = ("workspace", "user", "host")
DEFAULT_STATE_DIR: Final = Path("~/.local/state/ahp-host-flow")
PROVIDER_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
LOAD_SPEC: Final = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_][\w.]*")


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
    # 'module:Class', imported and constructed on first use, not at startup.
    load: str
    # Handed to the provider: a path to its own config file, or a table.
    config: Mapping[str, Any] | Path | None = None
    # Alias -> model id.
    models: Mapping[str, str] = field(default_factory=dict)
    # Passed verbatim to the inner session.
    approval_mode: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class WorkflowsConfig:
    dirs: tuple[Path, ...] = ()
    sources: tuple[str, ...] = SOURCES


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
    _check_keys(table, ("load", "config", "models", "approval_mode", "effort"), where)
    load = _str(table, "load", where)
    if load is None:
        raise ConfigError(f"{where}.load is required")
    if not LOAD_SPEC.fullmatch(load):
        raise ConfigError(f"{where}.load must be 'module:Class', got {load!r}")
    config: Mapping[str, Any] | Path | None = None
    raw_config = table.get("config")
    if isinstance(raw_config, str):
        config = _path(raw_config, base)
    elif isinstance(raw_config, Mapping):
        config = dict(raw_config)
    elif raw_config is not None:
        raise ConfigError(f"{where}.config must be a path or a table")
    models: dict[str, str] = {}
    for alias, model_id in _table(table.get("models", {}), f"{where}.models").items():
        if not isinstance(model_id, str):
            raise ConfigError(f"{where}.models.{alias} must be a string")
        models[alias] = model_id
    approval_mode = _str(table, "approval_mode", where)
    if approval_mode is not None and not approval_mode:
        raise ConfigError(f"{where}.approval_mode must not be empty")
    effort = _str(table, "effort", where)
    if effort is not None and effort not in EFFORT_LEVELS:
        raise ConfigError(
            f"{where}.effort must be one of {', '.join(EFFORT_LEVELS)}, got {effort!r}"
        )
    return ProviderConfig(name, load, config, models, approval_mode, effort)


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
    _check_keys(table, ("dirs", "sources"), "workflows")
    dirs = tuple(_path(d, base) for d in _str_list(table.get("dirs", []), "workflows.dirs"))
    sources = tuple(_str_list(table.get("sources", list(SOURCES)), "workflows.sources"))
    for source in sources:
        if source not in SOURCES:
            raise ConfigError(f"workflows.sources: {source!r} is not one of {', '.join(SOURCES)}")
    if len(set(sources)) != len(sources):
        raise ConfigError("workflows.sources has duplicates")
    return WorkflowsConfig(dirs, sources)


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
