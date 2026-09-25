"""Agent types this package provides itself: the offline echo agent, for tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_host_server.node.runner import NodeContext
from agent_host_server.provider import EchoProvider


def echo(options: Mapping[str, Any], node: NodeContext) -> EchoProvider:
    """`type = "echo"`: repeats each message back. Options: `provider_id`, `agent_name`."""
    return EchoProvider(
        provider_id=str(options.get("provider_id", "echo")),
        display_name=str(options.get("agent_name", "Echo")),
    )
