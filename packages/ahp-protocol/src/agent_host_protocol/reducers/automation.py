"""Automation catalogue channel reducer (0.9.0).

Ported from ``types/channels-automation/reducer.ts``. The catalogue is the
singleton ``ahp-automations://`` channel; its state is one ``entries`` list keyed
by each entry's ``resource`` (an ``ahp-automation:`` URI).

``automation/createRequested`` and ``automation/updateRequested`` are requests
*to the host*: the reducer leaves state alone, and the host answers by
dispatching ``automation/set``. ``automation/removed`` is client-dispatchable,
so its ``resource`` is read with :func:`get` -- an absent one is ``undefined``
and matches only an entry that has no ``resource`` either.

Where upstream would throw -- ``state.entries`` missing, or
``action.automation.resource`` read off a missing payload -- this port returns
the state unchanged, the branch's own no-op (invariant 3: never raise).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_host_protocol.reducers.js import get, index_of

__all__ = ["automation_reducer"]


def automation_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    action_type = action.get("type")

    if action_type in ("automation/createRequested", "automation/updateRequested"):
        return state

    if action_type == "automation/set":
        entries = get(state, "entries")
        automation = action.get("automation")
        if not isinstance(entries, list) or not isinstance(automation, Mapping):
            return state
        index = index_of(entries, "resource", get(automation, "resource"))
        if index < 0:
            return {**state, "entries": [*entries, automation]}
        updated = list(entries)
        updated[index] = automation
        return {**state, "entries": updated}

    if action_type == "automation/removed":
        entries = get(state, "entries")
        if not isinstance(entries, list):
            return state
        index = index_of(entries, "resource", get(action, "resource"))
        if index < 0:
            return state
        remaining = list(entries)
        del remaining[index]
        return {**state, "entries": remaining}

    # Unknown action: return the state unchanged. Never raise (softAssertNever).
    return state
