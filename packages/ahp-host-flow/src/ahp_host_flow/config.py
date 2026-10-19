"""`flow.toml`: providers, routing and workflow sources (plan §5)."""

from __future__ import annotations

import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from ahp_host.provider.policy import PolicyError, ToolPolicy, policy_from_table

from ahp_host_flow.engine.redact import Redactor
from ahp_host_flow.engine.registry import WORKFLOW_SOURCES
from ahp_host_flow.engine.verbnames import verb_name_error

# The names VS Code recognises; copied from ahp-host-claude, which a provider package cannot be
# imported sideways for.
APPROVAL_KEY: Final = "permissionMode"
EFFORT_KEY: Final = "effort"
APPROVAL_MODES: Final = ("default", "acceptEdits", "auto", "plan")
EFFORT_LEVELS: Final = ("low", "medium", "high", "xhigh", "max")
# Precedence order, highest first (plan §4.3).
SOURCES: Final = WORKFLOW_SOURCES
# Client-published workflows are opt-in: a shared host does not run what a connecting client sends
# unless the operator lists `client` (plan §4.3 Policy).
SOURCES_DEFAULT: Final = ("workspace", "user", "host")
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
    # Remote: seconds a relayed approval or question may wait for an answer on the worker
    # chat before the call fails. None: wait until the run is aborted.
    approval_timeout: float | None = None
    # In-process: worker chats of this provider take steering (plan §4.5).
    interactive: bool = False
    # Text put before every prompt the inner agent is sent: True for the default, a string for
    # one of your own.
    preamble: bool | str = True

    @property
    def kind(self) -> Literal["inprocess", "fleet", "remote"]:
        if self.load is not None:
            return "inprocess"
        return "fleet" if self.via is not None else "remote"


@dataclass(frozen=True)
class WorkflowsConfig:
    dirs: tuple[Path, ...] = ()
    sources: tuple[str, ...] = SOURCES_DEFAULT
    python_sources: tuple[str, ...] = PYTHON_SOURCES_DEFAULT
    # Seconds between scans of the local source dirs; 0 turns watching off.
    watch_interval: float = 2.0


@dataclass(frozen=True)
class RedactConfig:
    # Extra regexes, applied after the builtin credential shapes.
    patterns: tuple[str, ...] = ()
    builtin: bool = True


@dataclass(frozen=True)
class ResumeConfig:
    # 'default', 'model' (the routed provider and model join each key) or 'module:function'.
    key: str = "default"


@dataclass(frozen=True)
class ChangesetsConfig:
    # The branch a Commit creates; `{runId}` and `{ordinal}` are replaced.
    branch: str = "flow/{runId}-{ordinal}"
    # Add a `Flow-Principal:` trailer naming who pressed Commit.
    trailer: bool = False


@dataclass(frozen=True)
class ApprovalRule:
    """One `[[approvals.rules]]` entry: every key present must match; the first match decides."""

    action: Literal["allow", "deny"]
    tool: str | None = None
    read_only: bool | None = None
    provider: str | None = None
    phase: str | None = None
    persons: tuple[str, ...] | None = None
    #: Whether `read_only` may match a hint the agent supplied itself
    #: (`ToolConfirmation.read_only_from_agent`, ACP's `toolCall.kind`); off by default.
    trust_agent_hints: bool = False


@dataclass(frozen=True)
class ApprovalsConfig:
    rules: tuple[ApprovalRule, ...] = ()


@dataclass(frozen=True)
class FlowConfig:
    # In TOML order.
    providers: Mapping[str, ProviderConfig]
    routing: Routing
    state_dir: Path
    node: str | None = None
    workflows: WorkflowsConfig = WorkflowsConfig()
    redact: RedactConfig = RedactConfig()
    # Custom verbs: global name -> 'module:function', imported on the first call, in TOML order.
    verbs: Mapping[str, str] = field(default_factory=dict)
    resume: ResumeConfig = ResumeConfig()
    approvals: ApprovalsConfig = ApprovalsConfig()
    # Checked before the approval rules and always wins over them.
    policy: ToolPolicy | None = None
    changesets: ChangesetsConfig = ChangesetsConfig()


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
        allowed: tuple[str, ...] = (
            "load",
            "config",
            "models",
            "approval_mode",
            "effort",
            "interactive",
            "preamble",
        )
    elif "via" in table:
        kind = "fleet"
        allowed = (
            "via",
            "node",
            "agent",
            "models",
            "approval_mode",
            "effort",
            "reply_timeout",
            "preamble",
        )
    else:
        kind = "remote"
        allowed = (
            "url",
            "token_file",
            "agent",
            "models",
            "idle_timeout",
            "approval_timeout",
            "preamble",
        )
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
    interactive = table.get("interactive", False)
    if not isinstance(interactive, bool):
        raise ConfigError(f"{where}.interactive must be true or false")
    preamble = table.get("preamble", True)
    if not isinstance(preamble, bool) and (not isinstance(preamble, str) or not preamble.strip()):
        raise ConfigError(f"{where}.preamble must be true, false or a non-empty string")
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
        approval_timeout=_seconds(table, "approval_timeout", where),
        interactive=interactive,
        preamble=preamble,
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
    _check_keys(table, ("dirs", "sources", "python_sources", "watch_interval"), "workflows")
    dirs = tuple(_path(d, base) for d in _str_list(table.get("dirs", []), "workflows.dirs"))
    sources = tuple(_str_list(table.get("sources", list(SOURCES_DEFAULT)), "workflows.sources"))
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
        if source == "client":
            raise ConfigError(
                "workflows.python_sources: 'client' is never allowed: "
                "a client cannot supply a .py workflow"
            )
        if source not in SOURCES:
            raise ConfigError(
                f"workflows.python_sources: {source!r} is not one of {', '.join(SOURCES)}"
            )
    if len(set(python_sources)) != len(python_sources):
        raise ConfigError("workflows.python_sources has duplicates")
    interval = table.get("watch_interval", WorkflowsConfig.watch_interval)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int | float)
        or not math.isfinite(interval)
        or interval < 0
    ):
        raise ConfigError(
            "workflows.watch_interval must be a number of seconds >= 0 (0 turns watching off)"
        )
    return WorkflowsConfig(dirs, sources, python_sources, float(interval))


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


def _parse_verbs(raw: Any) -> dict[str, str]:
    table = _table(raw, "verbs")
    verbs: dict[str, str] = {}
    for name, value in table.items():
        reason = verb_name_error(name)
        if reason is not None:
            raise ConfigError(f"verbs.{name}: {reason}")
        if not isinstance(value, str) or not LOAD_SPEC.fullmatch(value):
            raise ConfigError(f"verbs.{name} must be 'module:function', got {value!r}")
        verbs[name] = value
    return verbs


def _parse_resume(raw: Any) -> ResumeConfig:
    table = _table(raw, "resume")
    _check_keys(table, ("key",), "resume")
    key = table.get("key", "default")
    if not isinstance(key, str) or not (key in ("default", "model") or LOAD_SPEC.fullmatch(key)):
        raise ConfigError(
            f'resume.key must be "default", "model" or \'module:function\', got {key!r}'
        )
    return ResumeConfig(key)


def _parse_changesets(raw: Any) -> ChangesetsConfig:
    table = _table(raw, "changesets")
    _check_keys(table, ("branch", "trailer"), "changesets")
    branch = table.get("branch", ChangesetsConfig.branch)
    if not isinstance(branch, str) or not branch.strip():
        raise ConfigError(f"changesets.branch must be a non-empty string, got {branch!r}")
    trailer = table.get("trailer", ChangesetsConfig.trailer)
    if not isinstance(trailer, bool):
        raise ConfigError(f"changesets.trailer must be a boolean, got {trailer!r}")
    return ChangesetsConfig(branch, trailer)


def _parse_approvals(raw: Any, providers: Mapping[str, ProviderConfig]) -> ApprovalsConfig:
    table = _table(raw, "approvals")
    _check_keys(table, ("rules",), "approvals")
    raw_rules = table.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigError("approvals.rules must be an array of tables")
    rules: list[ApprovalRule] = []
    for i, raw_rule in enumerate(raw_rules):
        where = f"approvals.rules[{i}]"
        rule = _table(raw_rule, where)
        _check_keys(
            rule,
            ("tool", "read_only", "provider", "phase", "persons", "action", "trust_agent_hints"),
            where,
        )
        action = _str(rule, "action", where)
        if action == "allow":
            verdict: Literal["allow", "deny"] = "allow"
        elif action == "deny":
            verdict = "deny"
        else:
            raise ConfigError(f"{where}.action must be 'allow' or 'deny', got {action!r}")
        tool = _str(rule, "tool", where)
        phase = _str(rule, "phase", where)
        for key, value in (("tool", tool), ("phase", phase)):
            if value is not None and not value:
                raise ConfigError(f"{where}.{key} must not be empty")
        read_only = rule.get("read_only")
        if read_only is not None and not isinstance(read_only, bool):
            raise ConfigError(f"{where}.read_only must be true or false")
        trust = rule.get("trust_agent_hints", False)
        if not isinstance(trust, bool):
            raise ConfigError(f"{where}.trust_agent_hints must be true or false")
        provider = _str(rule, "provider", where)
        if provider is not None and provider not in providers:
            raise ConfigError(f"{where}.provider names unknown provider {provider!r}")
        persons: tuple[str, ...] | None = None
        if "persons" in rule:
            listed = _str_list(rule["persons"], f"{where}.persons")
            if not listed or not all(listed):
                raise ConfigError(f"{where}.persons must be a non-empty array of non-empty strings")
            persons = tuple(listed)
        if tool is None and read_only is None and provider is None and phase is None:
            raise ConfigError(
                f'{where} needs at least one of tool, read_only, provider, phase (tool = "*" '
                "matches every call)"
            )
        rules.append(ApprovalRule(verdict, tool, read_only, provider, phase, persons, trust))
    return ApprovalsConfig(tuple(rules))


def _parse_policy(raw: Any) -> ToolPolicy:
    table = _table(raw, "policy")
    try:
        return policy_from_table(table)
    except PolicyError as exc:
        raise ConfigError(f"policy: {exc}") from exc


def make_redactor(redact: RedactConfig) -> Redactor:
    return Redactor(redact.patterns, builtin=redact.builtin)


def parse_config(data: Mapping[str, Any], *, base: Path) -> FlowConfig:
    """Validate `data` (a parsed flow.toml); relative paths resolve against `base`."""
    _check_keys(
        data,
        (
            "state_dir",
            "node",
            "providers",
            "routing",
            "workflows",
            "redact",
            "verbs",
            "resume",
            "approvals",
            "policy",
            "changesets",
        ),
        "flow.toml",
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
    verbs = _parse_verbs(data.get("verbs", {}))
    resume = _parse_resume(data.get("resume", {}))
    approvals = _parse_approvals(data.get("approvals", {}), providers)
    policy = _parse_policy(data["policy"]) if "policy" in data else None
    changesets = _parse_changesets(data.get("changesets", {}))
    return FlowConfig(
        providers,
        routing,
        state_dir,
        node,
        workflows,
        redact,
        verbs,
        resume,
        approvals,
        policy,
        changesets,
    )


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
