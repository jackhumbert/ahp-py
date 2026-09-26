"""`type = "claude"` in an agent-host-node config.

    [[agents]]
    type = "claude"
    provider_id = "claude"     # the default; keep it the same on every machine
    agent_name = "Claude"      # the default
    remote_control = true      # default: whatever Claude Code does
    claude_ai_sessions = true  # list the account's other sessions; one node only

Registered as the `claude` entry in the `agent_host_server.agents` group, so
`agent-host-node` can serve Claude beside other agents from one host.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from agent_host_server.node import NodeContext

from agent_host_server_claude.claude_ai import Api
from agent_host_server_claude.config import DEFAULT_AGENT_NAME, DEFAULT_PROVIDER_ID
from agent_host_server_claude.provider import ClaudeProvider, discover, is_valid_provider_id

_OPTIONS = frozenset({"provider_id", "agent_name", "remote_control", "claude_ai_sessions"})


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
    claude_ai_sessions = options.get("claude_ai_sessions", False)
    if not isinstance(claude_ai_sessions, bool):
        raise ValueError("claude agent: claude_ai_sessions must be true or false")
    # Once, at start-up: the picker offers what Claude Code offers this account,
    # and new sessions go on claude.ai if Claude Code's own would.
    found = await discover(node.roots.primary)
    log = logging.getLogger(__name__)
    log.info("models: %s", ", ".join(m.id for m in found.models) or "none")
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
        state_dir=node.state_dir,
    )
