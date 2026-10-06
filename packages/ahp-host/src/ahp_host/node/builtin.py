"""Agent types this package provides itself: the offline echo agent, for tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ahp_host.node.runner import NodeContext
from ahp_host.provider import EchoProvider


def echo(options: Mapping[str, Any], node: NodeContext) -> EchoProvider:
    """`type = "echo"`: repeats each message back. Options: `provider_id`, `agent_name`."""
    return EchoProvider(
        provider_id=str(options.get("provider_id", "echo")),
        display_name=str(options.get("agent_name", "Echo")),
        # What echo's `complete()` answers to; the node passes no triggers of
        # its own, so each agent's declaration is what clients are told.
        completion_trigger_characters=("#",),
    )
