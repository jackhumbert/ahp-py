"""Resource-watch channel reducer.

Ported from ``types/channels-resource-watch/reducer.ts``. One action, and it is
a deliberate no-op.

Watches are event-pass-through: ``resourceWatch/changed`` carries the change
events, but the reducer keeps no history of them. The state tracks only the
watch descriptor, which is set at subscription time and never mutates over the
life of the watch. A host that accumulated change events here would grow the
snapshot without bound for a watch nobody is reading.

Upstream uses ``if``/``else`` rather than ``switch``/``softAssertNever`` here
purely because TypeScript will not narrow a single-variant discriminated union
to ``never``; the runtime behaviour is the same as every other reducer's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["resource_watch_reducer"]


def resource_watch_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    if action.get("type") == "resourceWatch/changed":
        return state

    # Unknown action: return the state unchanged. Never raise (softAssertNever).
    return state
