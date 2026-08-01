"""Pure state reducers, hand-ported from the upstream TypeScript.

Reducers cannot be generated -- no upstream generator emits them, and the Go,
Rust, Kotlin and Swift reducers all carry "hand-written port" headers. The
portability mechanism is the shared fixture corpus, which every official client
is gated on and which we consume unmodified
(``vendor/upstream/test-cases/reducers``).

A reducer takes a state and an action and returns the next state. It must:

* never mutate its input (the host's replay log and already-issued snapshots
  alias it);
* return the input unchanged for an unknown action type, never raise
  (``softAssertNever`` semantics -- forward compatibility is a protocol
  requirement, and the corpus tests it);
* read time only through :mod:`agent_host_server.reducers.clock`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from agent_host_server.reducers.chat import chat_reducer
from agent_host_server.reducers.root import root_reducer
from agent_host_server.reducers.session import session_reducer

__all__ = ["REDUCERS", "Reducer", "chat_reducer", "root_reducer", "session_reducer"]

Reducer = Callable[[Any, Mapping[str, Any]], Any]

#: Keyed by the fixture corpus's `reducer` field so the harness can dispatch.
REDUCERS: Final[dict[str, Reducer]] = {
    "root": root_reducer,
    "session": session_reducer,
    "chat": chat_reducer,
}
