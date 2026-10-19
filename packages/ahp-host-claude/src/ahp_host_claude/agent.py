"""`type = "claude"` in an ahp-node config.

    [[agents]]
    type = "claude"
    provider_id = "claude"     # the default; keep it the same on every machine
    agent_name = "Claude"      # the default
    remote_control = true      # default: whatever Claude Code does
    claude_ai_sessions = true  # this machine's other sessions; "all": every machine's
    chat_tools = ["WebSearch"] # default both web tools; may only narrow the list
    env_allow = ["PATH", "HOME"]  # default: Claude Code sees every variable

    [agents.policy]               # see the README, "Host policy"
    deny_commands = ["git push*"]

Registered as the `claude` entry in the `ahp_host.agents` group, so
`ahp-node` can serve Claude beside other agents from one host.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ahp_host.node import NodeContext
from ahp_host.provider.policy import PolicyError, ToolPolicy, combine_tables, policy_from_table

from ahp_host_claude.claude_ai import LOCAL, Api, scope_of
from ahp_host_claude.config import (
    DEFAULT_AGENT_NAME,
    DEFAULT_PROVIDER_ID,
    DEFAULT_STATE,
    SERVER_ONLY_KEYS,
    ConfigError,
    _path,
    _roots_from_file,
    read_config_file,
    roots_option,
)
from ahp_host_claude.provider import (
    CHAT_TOOLS,
    ClaudeProvider,
    ClientFactory,
    chat_tools_subset,
    discover,
    is_valid_provider_id,
)
from ahp_host_claude.roots import Roots

AGENT_OPTIONS = frozenset(
    {
        "provider_id",
        "agent_name",
        "remote_control",
        "claude_ai_sessions",
        "chat_tools",
        "env_allow",
        "policy",
    }
)


@dataclass(frozen=True)
class AgentOptions:
    """The per-agent options, checked."""

    provider_id: str
    agent_name: str
    #: None: whatever Claude Code itself does, learned at discovery.
    remote_control: bool | None
    #: `claude_ai.scope_of`'s answer: falsy is off.
    claude_ai_sessions: str | None
    chat_tools: tuple[str, ...]
    #: None: Claude Code sees every host variable.
    env_allow: tuple[str, ...] | None
    #: None: only the approval gate.
    policy: ToolPolicy | None


def parse_agent_options(options: Mapping[str, Any]) -> AgentOptions:
    unknown = sorted(set(options) - AGENT_OPTIONS)
    if unknown:
        raise ValueError(f"claude agent: unknown option(s): {', '.join(unknown)}")
    provider_id = str(options.get("provider_id", DEFAULT_PROVIDER_ID))
    if not is_valid_provider_id(provider_id):
        raise ValueError(f"provider id {provider_id!r}: use letters, digits, '-' and '_'")
    remote_control = options.get("remote_control")
    if remote_control is not None and not isinstance(remote_control, bool):
        raise ValueError("claude agent: remote_control must be true or false")
    chat_tools = options.get("chat_tools", CHAT_TOOLS)
    if not isinstance(chat_tools, (list, tuple)):
        raise ValueError("claude agent: chat_tools must be a list of tool names")
    try:
        chat_tools = chat_tools_subset(chat_tools)
    except ValueError as exc:
        raise ValueError(f"claude agent: {exc}") from exc
    try:
        claude_ai_sessions = scope_of(options.get("claude_ai_sessions", False))
    except ValueError as exc:
        raise ValueError(f"claude agent: {exc}") from exc
    env_allow = options.get("env_allow")
    if env_allow is not None and (
        not isinstance(env_allow, list | tuple)
        or not all(isinstance(v, str) and v for v in env_allow)
    ):
        raise ValueError(
            "claude agent: env_allow must be a list of variable names or glob patterns"
        )
    policy = options.get("policy")
    if policy is not None:
        if not isinstance(policy, Mapping):
            raise ValueError("claude agent: policy must be a table")
        try:
            policy = policy_from_table(policy)
        except PolicyError as exc:
            raise ValueError(f"claude agent: {exc}") from exc
    return AgentOptions(
        provider_id=provider_id,
        agent_name=str(options.get("agent_name", DEFAULT_AGENT_NAME)),
        remote_control=remote_control,
        claude_ai_sessions=claude_ai_sessions,
        chat_tools=tuple(chat_tools),
        env_allow=tuple(env_allow) if env_allow is not None else None,
        policy=policy,
    )


async def create(options: Mapping[str, Any], node: NodeContext) -> ClaudeProvider:
    opts = parse_agent_options(options)
    remote_control = opts.remote_control
    # Once, at start-up: the picker offers what Claude Code offers this account,
    # and new sessions go on claude.ai if Claude Code's own would.
    found = await discover(node.roots.primary, state_dir=node.state_dir)
    log = logging.getLogger(__name__)
    log.info("models: %s", ", ".join(m.id for m in found.models) or "none")
    if opts.claude_ai_sessions:
        # One line per request is a line every few seconds per claude.ai session.
        logging.getLogger("httpx").setLevel(logging.WARNING)
    if remote_control is None:
        remote_control = found.remote_control
    log.info("Remote Control for new sessions: %s", "on" if remote_control else "off")
    return ClaudeProvider(
        node.roots,
        display_name=opts.agent_name,
        models=found.models,
        provider_id=opts.provider_id,
        remote_control=remote_control,
        claude_ai=Api() if opts.claude_ai_sessions else None,
        claude_ai_scope=opts.claude_ai_sessions or LOCAL,
        state_dir=node.state_dir,
        chat_tools=opts.chat_tools,
        env_allow=opts.env_allow,
        policy=opts.policy,
        commands=found.commands,
        # Start-up could not say: try again until it can.
        rediscover=not found.models,
    )


def from_config(
    config: str | os.PathLike[str] | None = None,
    *,
    roots: Roots
    | str
    | os.PathLike[str]
    | Sequence[str | os.PathLike[str]]
    | Mapping[str, str | os.PathLike[str]]
    | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    policy: Mapping[str, Any] | None = None,
    client_factory: ClientFactory | None = None,
    **options: Any,
) -> ClaudeProvider:
    """A provider from a config file and/or keywords, with no node; keywords win.

    Synchronous and connects to nothing: Claude Code is asked for its models
    (and its Remote Control default) on the first session.
    """
    data = read_config_file(Path(config).expanduser()) if config is not None else {}
    # The file's server-only keys (port, token_file, ...) are ignored. As keywords they stay
    # unknown options and are refused by `parse_agent_options`. What is left of the file, minus
    # the node-level keys (root, roots, state_dir) read below, is the per-agent options.
    from_file = {key: value for key, value in data.items() if key not in SERVER_ONLY_KEYS}
    merged = {key: value for key, value in from_file.items() if key in AGENT_OPTIONS}
    if merged.get("claude_ai_sessions") == "off":
        merged["claude_ai_sessions"] = False
    merged.update(options)
    if policy is not None:
        # A named keyword (not just **options) so a host that composes policies, such as
        # ahp-host-flow's loader, can see this factory takes one. It is combined with the
        # file's own [policy], never put in its place: a deny from either wins.
        own = merged.get("policy")
        if isinstance(own, Mapping) and isinstance(policy, Mapping):
            try:
                merged["policy"] = combine_tables(own, policy)
            except PolicyError as exc:
                raise ValueError(f"claude agent: {exc}") from exc
        else:
            merged["policy"] = policy
    opts = parse_agent_options(merged)
    served = roots_option(roots) if roots is not None else _roots_from_file(data)
    if served is None:
        raise ConfigError(
            "no folder to serve: pass roots=, or set root / [roots] in the config file"
        )
    for path in served.paths:
        if not path.is_dir():
            raise ConfigError(f"{path} is not a directory")
    if state_dir is not None:
        state = Path(state_dir).expanduser()
    elif "state_dir" in data:
        state = _path(data["state_dir"], "state_dir")
    else:
        state = DEFAULT_STATE
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    extra: dict[str, Any] = {}
    if client_factory is not None:
        extra["client_factory"] = client_factory
    return ClaudeProvider(
        served,
        display_name=opts.agent_name,
        models=(),
        provider_id=opts.provider_id,
        remote_control=opts.remote_control,
        claude_ai=Api() if opts.claude_ai_sessions else None,
        claude_ai_scope=opts.claude_ai_sessions or LOCAL,
        state_dir=state,
        chat_tools=opts.chat_tools,
        env_allow=opts.env_allow,
        policy=opts.policy,
        commands=(),
        rediscover=True,
        lazy_discovery=True,
        **extra,
    )
