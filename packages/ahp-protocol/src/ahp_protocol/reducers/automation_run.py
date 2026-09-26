"""Automation-run channel reducer (0.9.0).

Ported from ``types/channels-automation-run/reducer.ts``. One run of one
automation, on an ``ahp-automation-run:`` channel: its ``lifecycle``, the
``sessions`` it spawned, and which of them is ``primarySession``.

``automationRun/cancelRequested`` is the one client-dispatchable action, and it
is a request to the host -- the reducer leaves state alone.

Two JS details carried over deliberately:

* ``primarySessionChanged`` distinguishes ``undefined`` (delete the key) from an
  explicit ``null`` (store it), so the action is read with :func:`get`;
* ``sessionSet`` with no ``session`` appends an ``undefined`` array element,
  which ``JSON.stringify`` writes as ``null``.

A missing ``sessions`` list would make upstream throw; this port returns the
state unchanged instead (invariant 3: never raise).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ahp_protocol.reducers.js import UNDEFINED, assign, get, index_of_value, strict_equal

__all__ = ["automation_run_reducer"]


def automation_run_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    action_type = action.get("type")

    if action_type == "automationRun/lifecycleChanged":
        return assign({**state}, "lifecycle", get(action, "lifecycle"))

    if action_type == "automationRun/sessionSet":
        sessions = get(state, "sessions")
        if not isinstance(sessions, list):
            return state
        session = get(action, "session")
        if index_of_value(sessions, session) >= 0:
            return state
        appended = None if session is UNDEFINED else session
        return {**state, "sessions": [*sessions, appended]}

    if action_type == "automationRun/sessionRemoved":
        sessions = get(state, "sessions")
        if not isinstance(sessions, list):
            return state
        session = get(action, "session")
        index = index_of_value(sessions, session)
        if index < 0:
            return state
        remaining = list(sessions)
        del remaining[index]
        next_state = {**state, "sessions": remaining}
        if strict_equal(get(state, "primarySession"), session):
            next_state.pop("primarySession", None)
        return next_state

    if action_type == "automationRun/primarySessionChanged":
        return assign({**state}, "primarySession", get(action, "primarySession"))

    if action_type == "automationRun/cancelRequested":
        return state

    # Unknown action: return the state unchanged. Never raise (softAssertNever).
    return state
