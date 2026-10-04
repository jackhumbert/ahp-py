"""Canvas channel reducer (1.0.0, experimental).

Ported from ``types/channels-canvas/reducer.ts``. One live canvas, on an
``ahp-canvas:`` channel a chat advertises through ``ChatState.canvases``.

``canvas/stateChanged`` is the only action and it replaces the whole state, so
dropping ``url`` from the replacement is how a host withdraws a live source.

An action without ``canvas`` would make upstream return ``undefined`` as the
next state; this port returns the state unchanged instead (invariant 3: never
produce something that is not a state).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ahp_protocol.reducers.js import UNDEFINED, get

__all__ = ["canvas_reducer"]


def canvas_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    if action.get("type") == "canvas/stateChanged":
        canvas = get(action, "canvas")
        if canvas is UNDEFINED:
            return state
        return canvas

    # Unknown action: return the state unchanged. Never raise (softAssertNever).
    return state
