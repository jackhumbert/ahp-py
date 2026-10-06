"""`type = "claude"` in an ahp-node config.

    [[agents]]
    type = "claude"
    provider_id = "claude"     # the default; keep it the same on every machine
    agent_name = "Claude"      # the default
    remote_control = true      # default: whatever Claude Code does
    claude_ai_sessions = true  # this machine's other sessions; "all": every machine's
    chat_tools = ["WebSearch"] # default both web tools; may only narrow the list

Registered as the `claude` entry in the `ahp_host.agents` group, so
`ahp-node` can serve Claude beside other agents from one host.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ahp_host.node import NodeContext

from ahp_host_claude.claude_ai import LOCAL, Api, scope_of
from ahp_host_claude.config import DEFAULT_AGENT_NAME, DEFAULT_PROVIDER_ID
from ahp_host_claude.provider import (
    CHAT_TOOLS,
    ClaudeProvider,
    chat_tools_subset,
    discover,
    is_valid_provider_id,
)

_OPTIONS = frozenset(
    {"provider_id", "agent_name", "remote_control", "claude_ai_sessions", "chat_tools"}
)


async def create(options: Mapping[str, Any], node: NodeContext) -> ClaudeProvider:
    unknown = sorted(set(options) - _OPTIONS)
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
    # Once, at start-up: the picker offers what Claude Code offers this account,
    # and new sessions go on claude.ai if Claude Code's own would.
    found = await discover(node.roots.primary, state_dir=node.state_dir)
    log = logging.getLogger(__name__)
    log.info("models: %s", ", ".join(m.id for m in found.models) or "none")
    if claude_ai_sessions:
        # One line per request is a line every few seconds per claude.ai session.
        logging.getLogger("httpx").setLevel(logging.WARNING)
    if remote_control is None:
        remote_control = found.remote_control
    log.info("Remote Control for new sessions: %s", "on" if remote_control else "off")
    return ClaudeProvider(
        node.roots,
        display_name=str(options.get("agent_name", DEFAULT_AGENT_NAME)),
        models=found.models,
        provider_id=provider_id,
        remote_control=remote_control,
        claude_ai=Api() if claude_ai_sessions else None,
        claude_ai_scope=claude_ai_sessions or LOCAL,
        state_dir=node.state_dir,
        chat_tools=chat_tools,
        commands=found.commands,
        # Start-up could not say: try again until it can.
        rediscover=not found.models,
    )
