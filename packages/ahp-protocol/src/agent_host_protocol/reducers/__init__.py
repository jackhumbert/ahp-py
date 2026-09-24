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
* read time only through :mod:`agent_host_protocol.reducers.clock`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from agent_host_protocol.reducers.annotations import annotations_reducer
from agent_host_protocol.reducers.automation import automation_reducer
from agent_host_protocol.reducers.automation_run import automation_run_reducer
from agent_host_protocol.reducers.changeset import changeset_reducer
from agent_host_protocol.reducers.chat import chat_reducer
from agent_host_protocol.reducers.resource_watch import resource_watch_reducer
from agent_host_protocol.reducers.root import root_reducer
from agent_host_protocol.reducers.session import session_reducer
from agent_host_protocol.reducers.terminal import terminal_reducer

__all__ = [
    "REDUCERS",
    "Reducer",
    "annotations_reducer",
    "automation_reducer",
    "automation_run_reducer",
    "changeset_reducer",
    "chat_reducer",
    "resource_watch_reducer",
    "root_reducer",
    "session_reducer",
    "terminal_reducer",
]

Reducer = Callable[[Any, Mapping[str, Any]], Any]

#: Keyed by the fixture corpus's `reducer` field so the harness can dispatch.
#: All nine, so the whole 272-fixture corpus runs -- ADR 0004 makes the port
#: all-or-nothing per channel, and registering a channel whose reducer does not
#: exist would leave its client-dispatchable actions unreduced.
REDUCERS: Final[dict[str, Reducer]] = {
    "root": root_reducer,
    "session": session_reducer,
    "chat": chat_reducer,
    "terminal": terminal_reducer,
    "changeset": changeset_reducer,
    "annotations": annotations_reducer,
    "resourceWatch": resource_watch_reducer,
    "automation": automation_reducer,
    "automationRun": automation_run_reducer,
}
