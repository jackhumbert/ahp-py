"""Root channel reducer.

Ported from ``types/channels-root/reducer.ts``. Four actions, full-replacement
semantics, and a hard no-op on ``root/configChanged`` when no config exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["root_reducer"]


def root_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    action_type = action.get("type")

    if action_type == "root/agentsChanged":
        return {**state, "agents": action["agents"]}

    if action_type == "root/activeSessionsChanged":
        return {**state, "activeSessions": action["activeSessions"]}

    if action_type == "root/terminalsChanged":
        return {**state, "terminals": action["terminals"]}

    if action_type == "root/configChanged":
        config = state.get("config")
        # Upstream: `if (!state.config) return state;` -- a config change against
        # a host that publishes no config schema is dropped, not created.
        if config is None:
            return state
        values = (
            {**action["config"]}
            if action.get("replace")
            else {**config.get("values", {}), **action["config"]}
        )
        return {**state, "config": {**config, "values": values}}

    # Unknown action: return the state unchanged. Never raise -- a client or host
    # speaking an older version must degrade gracefully (softAssertNever).
    return state
