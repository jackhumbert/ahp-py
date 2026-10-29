"""`flow.toml`: providers, routing and workflow sources (plan §5)."""

from __future__ import annotations

import math
import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from ahp_host.provider.policy import PolicyError, ToolPolicy, policy_from_table

from ahp_host_flow.engine.quota import QuotaLimits
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
    # Providers tried, in order, when the rule's provider fails in a way another could cure; None:
    # the provider's own `fallback`.
    fallback: tuple[str, ...] | None = None


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
    # Seconds a call may go without any event (reasoning counts; an open approval pauses the
    # clock) before it is stopped and fails with kind 'timeout'. Remote: None is
    # `remote.IDLE_TIMEOUT`. In-process and fleet: None is no watchdog (a fleet call sees status
    # changes only, not deltas).
    idle_timeout: float | None = None
    # Remote: seconds a relayed approval or question may wait for an answer on the worker
    # chat before the call fails. None: wait until the run is aborted.
    approval_timeout: float | None = None
    # Seconds one agent() call to this provider may run before it is stopped and fails with kind
    # 'timeout'; `opts.x.timeout` overrides it per call. None: no limit.
    call_timeout: float | None = None
    # Extra attempts after an empty reply (no text, or no value for a schema); 0 returns the
    # empty reply as it is.
    retry_empty: int = 0
    # Calls of this provider that may run at once, across every run of the flow provider: an
    # integer >= 1, or 'probe' (in-process only: two trial turns decide between 1 and no limit).
    # None: no limit.
    concurrency: int | Literal["probe"] | None = None
    # Providers tried in order, each with its own fallback, when a call to this one fails as
    # unavailable, rate limited, timed out, empty or transiently failed.
    fallback: tuple[str, ...] = ()
    # In-process: worker chats of this provider take steering (plan §4.5).
    interactive: bool = False
    # Text put before every prompt the inner agent is sent: True for the default, a string for
    # one of your own.
    preamble: bool | str = True
    # Remote: 'module:function', an async factory that starts a host for the call (or the run)
    # and returns its transport; replaces `url`.
    launch: str | None = None
    # Remote with launch: start a host for every call, and tear it down when the call ends.
    per_call: bool = False

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
    # Client tools offered to workers: all of the session's, none, or only these names.
    client_tools: bool | tuple[str, ...] = True


@dataclass(frozen=True)
class RedactConfig:
    # Extra regexes, applied after the builtin credential shapes.
    patterns: tuple[str, ...] = ()
    builtin: bool = True


@dataclass(frozen=True)
class JournalConfig:
    # Keep the script, its args and child workflow sources in the journal as they are, rather
    # than masked with their sha256 (the default).
    keep_source: bool = False


@dataclass(frozen=True)
class ResumeConfig:
    # 'default', 'model' (the routed provider and model join each key) or 'module:function'.
    key: str = "default"
    # Whether a client may start a turn that continues an earlier run of this host
    # (`continueRun`); TOML `continue`. A restart's own resume is not affected.
    continue_run: bool = True


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
class LimitsConfig:
    """`[limits]`: run-wide caps for runs started through the provider (plan M16)."""

    # Output tokens, input tokens and USD a run may use; None: no cap.
    budget: int | None = None
    input_budget: int | None = None
    cost_budget: float | None = None
    # Seconds of wall clock before in-flight calls are stopped and new ones refused.
    wall_budget: float | None = None
    # Seconds before the whole run is killed (like `flow run --timeout`).
    timeout: float | None = None
    concurrency: int | None = None
    # Output tokens reserved per admitted call: a count, or 'mean' for the run's mean so far.
    # None: the RunOptions default.
    reserve: int | Literal["mean"] | None = None
    count_cache: bool = True
    # Seconds between the main chat's heartbeat while a step is in flight (below a client's idle
    # timeout, 300 s by default).
    heartbeat: float = 30.0


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
    limits: LimitsConfig = LimitsConfig()
    #: `[quotas]`: per-principal caps, kept in `<state_dir>/quotas.json`.
    quotas: QuotaLimits = field(default_factory=QuotaLimits)
    journal: JournalConfig = JournalConfig()


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


def _is_number(value: Any, *, integer: bool = False) -> bool:
    """Whether *value* is a finite number (an int, with *integer*); a bool never is.

    The one check behind every numeric option: TOML has `nan` and `inf`, which no limit,
    timeout or budget may be.
    """
    if isinstance(value, bool):
        return False
    if integer:
        return isinstance(value, int)
    return isinstance(value, int | float) and math.isfinite(value)


def _seconds(table: Mapping[str, Any], key: str, where: str) -> float | None:
    value = table.get(key)
    if value is None:
        return None
    if not _is_number(value) or value <= 0:
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
    present = [key for key in ("load", "via", "url", "launch") if key in table]
    if not present:
        raise ConfigError(f"{where} needs one of load, via, url, launch")
    if len(present) > 1:
        raise ConfigError(f"{where}: load, via, url and launch are exclusive")
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
            "idle_timeout",
            "call_timeout",
            "retry_empty",
            "concurrency",
            "fallback",
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
            "idle_timeout",
            "call_timeout",
            "retry_empty",
            "concurrency",
            "fallback",
        )
    else:
        kind = "remote"
        allowed = (
            "url",
            "launch",
            "per_call",
            "token_file",
            "agent",
            "models",
            "idle_timeout",
            "approval_timeout",
            "preamble",
            "call_timeout",
            "retry_empty",
            "concurrency",
            "fallback",
        )
    _check_keys(table, allowed, where)
    load = via = node = agent = url = launch = None
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
        if "launch" in table:
            launch = _str(table, "launch", where)
            if launch is None or not LOAD_SPEC.fullmatch(launch):
                raise ConfigError(f"{where}.launch must be 'module:function', got {launch!r}")
            if "token_file" in table:
                raise ConfigError(
                    f"{where}.token_file needs url "
                    "(a launched host's transport comes from the launcher)"
                )
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
    per_call = table.get("per_call", False)
    if not isinstance(per_call, bool):
        raise ConfigError(f"{where}.per_call must be true or false")
    if per_call and launch is None:
        raise ConfigError(f"{where}.per_call needs launch")
    preamble = table.get("preamble", True)
    if not isinstance(preamble, bool) and (not isinstance(preamble, str) or not preamble.strip()):
        raise ConfigError(f"{where}.preamble must be true, false or a non-empty string")
    retry_empty = table.get("retry_empty", 0)
    if not _is_number(retry_empty, integer=True) or retry_empty < 0:
        raise ConfigError(f"{where}.retry_empty must be an integer >= 0")
    concurrency = table.get("concurrency")
    if concurrency == "probe":
        if kind != "inprocess":
            raise ConfigError(f'{where}.concurrency = "probe" needs an in-process provider (load)')
    elif concurrency is not None and (not _is_number(concurrency, integer=True) or concurrency < 1):
        raise ConfigError(f'{where}.concurrency must be an integer >= 1 or "probe"')
    fallback = tuple(_str_list(table.get("fallback", []), f"{where}.fallback"))
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
        call_timeout=_seconds(table, "call_timeout", where),
        retry_empty=retry_empty,
        concurrency=concurrency,
        fallback=fallback,
        interactive=interactive,
        preamble=preamble,
        launch=launch,
        per_call=per_call,
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
        _check_keys(rule, ("provider", "model", "phase", "label", "fallback"), where)
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
        fallback = (
            tuple(_str_list(rule["fallback"], f"{where}.fallback")) if "fallback" in rule else None
        )
        if fallback is not None:
            for target in fallback:
                if target not in providers:
                    raise ConfigError(f"{where}.fallback names unknown provider {target!r}")
        rules.append(RoutingRule(provider, model, phase, label, fallback))
    return Routing(default, tuple(rules))


def _check_fallbacks(providers: Mapping[str, ProviderConfig], routing: Routing) -> None:
    """Every fallback names a configured provider, and the fallback edges (each provider's, and
    each rule's from its provider) have no cycle."""
    edges: dict[str, list[str]] = {}
    for name, spec in providers.items():
        for target in spec.fallback:
            if target not in providers:
                raise ConfigError(f"providers.{name}.fallback names unknown provider {target!r}")
            if target == name:
                raise ConfigError(f"providers.{name}.fallback lists {name!r} itself")
        edges.setdefault(name, []).extend(spec.fallback)
    for rule in routing.rules:
        if rule.fallback:
            edges.setdefault(rule.provider, []).extend(rule.fallback)
    done: set[str] = set()

    def visit(node: str, path: list[str]) -> None:
        if node in path:
            cycle = [*path[path.index(node) :], node]
            raise ConfigError(f"fallback cycle: {' -> '.join(cycle)}")
        if node in done:
            return
        for target in edges.get(node, ()):
            visit(target, [*path, node])
        done.add(node)

    for name in edges:
        visit(name, [])


def _parse_workflows(raw: Any, base: Path) -> WorkflowsConfig:
    table = _table(raw, "workflows")
    _check_keys(
        table, ("dirs", "sources", "python_sources", "watch_interval", "client_tools"), "workflows"
    )
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
    if not _is_number(interval) or interval < 0:
        raise ConfigError(
            "workflows.watch_interval must be a number of seconds >= 0 (0 turns watching off)"
        )
    client_tools: bool | tuple[str, ...] = True
    if "client_tools" in table:
        raw_tools = table["client_tools"]
        if isinstance(raw_tools, bool):
            client_tools = raw_tools
        elif isinstance(raw_tools, list) and all(isinstance(t, str) for t in raw_tools):
            client_tools = tuple(raw_tools)
            if len(set(client_tools)) != len(client_tools):
                raise ConfigError("workflows.client_tools has duplicates")
        else:
            raise ConfigError(
                "workflows.client_tools must be true, false or an array of tool names"
            )
    return WorkflowsConfig(
        dirs, sources, python_sources, float(interval), client_tools=client_tools
    )


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


def _parse_journal(raw: Any) -> JournalConfig:
    table = _table(raw, "journal")
    _check_keys(table, ("keep_source",), "journal")
    keep_source = table.get("keep_source", False)
    if not isinstance(keep_source, bool):
        raise ConfigError("journal.keep_source must be true or false")
    return JournalConfig(keep_source)


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
    _check_keys(table, ("key", "continue"), "resume")
    key = table.get("key", "default")
    if not isinstance(key, str) or not (key in ("default", "model") or LOAD_SPEC.fullmatch(key)):
        raise ConfigError(
            f'resume.key must be "default", "model" or \'module:function\', got {key!r}'
        )
    continue_run = table.get("continue", True)
    if not isinstance(continue_run, bool):
        raise ConfigError("resume.continue must be true or false")
    return ResumeConfig(key, continue_run)


#: A run id to try `changesets.branch` with (the shape `engine.RUN_ID` allows).
_SAMPLE_RUN_ID: Final = "0123456789abcdef0123456789abcdef"


def _branch_name_ok(name: str) -> bool:
    """Whether git takes *name* as a branch name (True when git is not there to ask: a
    worktree needs git anyway, and Commit checks the name again)."""
    try:
        done = subprocess.run(
            ["git", "check-ref-format", "--branch", name],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return done.returncode == 0


def _parse_changesets(raw: Any) -> ChangesetsConfig:
    table = _table(raw, "changesets")
    _check_keys(table, ("branch", "trailer"), "changesets")
    branch = table.get("branch", ChangesetsConfig.branch)
    if not isinstance(branch, str) or not branch.strip():
        raise ConfigError(f"changesets.branch must be a non-empty string, got {branch!r}")
    if "{runId}" not in branch and "{ordinal}" not in branch:
        # A fixed name would be taken by the first Commit and refused for every later one.
        raise ConfigError(
            f"changesets.branch must contain {{runId}} or {{ordinal}}, got {branch!r}"
        )
    sample = branch.replace("{runId}", _SAMPLE_RUN_ID).replace("{ordinal}", "0")
    if not _branch_name_ok(sample):
        raise ConfigError(f"changesets.branch {branch!r} is not a valid branch name ({sample!r})")
    trailer = table.get("trailer", ChangesetsConfig.trailer)
    if not isinstance(trailer, bool):
        raise ConfigError(f"changesets.trailer must be a boolean, got {trailer!r}")
    return ChangesetsConfig(branch, trailer)


def _count(table: Mapping[str, Any], key: str, minimum: int, where: str) -> int | None:
    value = table.get(key)
    if value is None:
        return None
    if not _is_number(value, integer=True) or value < minimum:
        raise ConfigError(f"{where}.{key} must be an integer >= {minimum}")
    return int(value)


def _parse_limits(raw: Any) -> LimitsConfig:
    table = _table(raw, "limits")
    _check_keys(
        table,
        (
            "budget",
            "input_budget",
            "cost_budget",
            "wall_budget",
            "timeout",
            "concurrency",
            "reserve",
            "count_cache",
            "heartbeat",
        ),
        "limits",
    )
    cost = table.get("cost_budget")
    if cost is not None and (not _is_number(cost) or cost < 0):
        raise ConfigError("limits.cost_budget must be a number >= 0")
    reserve: int | Literal["mean"] | None
    reserve = "mean" if table.get("reserve") == "mean" else _count(table, "reserve", 0, "limits")
    count_cache = table.get("count_cache", LimitsConfig.count_cache)
    if not isinstance(count_cache, bool):
        raise ConfigError("limits.count_cache must be true or false")
    return LimitsConfig(
        budget=_count(table, "budget", 0, "limits"),
        input_budget=_count(table, "input_budget", 0, "limits"),
        cost_budget=None if cost is None else float(cost),
        wall_budget=_seconds(table, "wall_budget", "limits"),
        timeout=_seconds(table, "timeout", "limits"),
        concurrency=_count(table, "concurrency", 1, "limits"),
        reserve=reserve,
        count_cache=count_cache,
        heartbeat=_seconds(table, "heartbeat", "limits") or LimitsConfig.heartbeat,
    )


def _parse_quotas(raw: Any) -> QuotaLimits:
    table = _table(raw, "quotas")
    keys = ("tokens_per_day", "tokens_per_run", "cost_per_day", "cost_per_run")
    _check_keys(table, keys, "quotas")
    for key in keys[2:]:
        value = table.get(key)
        if value is not None and (not _is_number(value) or value <= 0):
            raise ConfigError(f"quotas.{key} must be a number > 0")
    cost_day, cost_run = table.get("cost_per_day"), table.get("cost_per_run")
    return QuotaLimits(
        tokens_per_day=_count(table, "tokens_per_day", 1, "quotas"),
        tokens_per_run=_count(table, "tokens_per_run", 1, "quotas"),
        cost_per_day=None if cost_day is None else float(cost_day),
        cost_per_run=None if cost_run is None else float(cost_run),
    )


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
            "limits",
            "quotas",
            "journal",
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
    _check_fallbacks(providers, routing)
    workflows = _parse_workflows(data.get("workflows", {}), base)
    redact = _parse_redact(data.get("redact", {}))
    verbs = _parse_verbs(data.get("verbs", {}))
    resume = _parse_resume(data.get("resume", {}))
    approvals = _parse_approvals(data.get("approvals", {}), providers)
    policy = _parse_policy(data["policy"]) if "policy" in data else None
    changesets = _parse_changesets(data.get("changesets", {}))
    limits = _parse_limits(data.get("limits", {}))
    quotas = _parse_quotas(data.get("quotas", {}))
    journal = _parse_journal(data.get("journal", {}))
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
        limits,
        quotas,
        journal,
    )


def load_config(path: Path) -> FlowConfig:
    path = path.expanduser()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        raise ConfigError(f"cannot read {path}: {reason}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_config(data, base=path.parent)
