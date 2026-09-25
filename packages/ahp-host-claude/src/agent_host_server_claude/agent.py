"""`type = "claude"` in an agent-host-node config.

    [[agents]]
    type = "claude"
    provider_id = "claude"     # the default; keep it the same on every machine
    agent_name = "Claude"      # the default

Registered as the `claude` entry in the `agent_host_server.agents` group, so
`agent-host-node` can serve Claude beside other agents from one host.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from agent_host_server.node import NodeContext

from agent_host_server_claude.config import DEFAULT_AGENT_NAME, DEFAULT_PROVIDER_ID
from agent_host_server_claude.provider import ClaudeProvider, discover_models, is_valid_provider_id

_OPTIONS = frozenset({"provider_id", "agent_name"})


async def create(options: Mapping[str, Any], node: NodeContext) -> ClaudeProvider:
    unknown = sorted(set(options) - _OPTIONS)
    if unknown:
        raise ValueError(f"claude agent: unknown option(s): {', '.join(unknown)}")
    provider_id = str(options.get("provider_id", DEFAULT_PROVIDER_ID))
    if not is_valid_provider_id(provider_id):
        raise ValueError(f"provider id {provider_id!r}: use letters, digits, '-' and '_'")
    # Once, at start-up: the picker offers what Claude Code offers this account.
    models = await discover_models(node.roots.primary)
    logging.getLogger(__name__).info("models: %s", ", ".join(m.id for m in models) or "none")
    return ClaudeProvider(
        node.roots,
        display_name=str(options.get("agent_name", DEFAULT_AGENT_NAME)),
        models=models,
        provider_id=provider_id,
    )
