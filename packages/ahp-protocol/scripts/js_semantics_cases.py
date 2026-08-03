"""Adversarial cases the fixture corpus cannot express.

Every case here targets one of the two defect classes the audit named:
undefined-vs-null on an unconditional spread, and `===` vs `==` on an id lookup.
"""

from __future__ import annotations

import json
import sys
from typing import Any

CASES: list[dict[str, Any]] = []


def case(name: str, reducer: str, initial: Any, actions: list[dict[str, Any]]) -> None:
    CASES.append({"name": name, "reducer": reducer, "initial": initial, "actions": actions})


# ── terminal: unconditional spread, explicit null vs absent ──────────────────

_TERM = {"claim": {"kind": "session"}, "content": [], "title": "bash", "cols": 80, "rows": 24}

case("term/title=null", "terminal", _TERM, [{"type": "terminal/titleChanged", "title": None}])
case("term/title-absent", "terminal", _TERM, [{"type": "terminal/titleChanged"}])
case("term/claim=null", "terminal", _TERM, [{"type": "terminal/claimed", "claim": None}])
case("term/claim-absent", "terminal", _TERM, [{"type": "terminal/claimed"}])
case("term/resize=null", "terminal", _TERM, [{"type": "terminal/resized", "cols": None}])
case("term/resize-absent", "terminal", _TERM, [{"type": "terminal/resized"}])
case("term/cwd=null", "terminal", _TERM, [{"type": "terminal/cwdChanged", "cwd": None}])
case(
    "term/exit=null",
    "terminal",
    {**_TERM, "exitCode": 3},
    [{"type": "terminal/exited", "exitCode": None}],
)
case("term/exit-absent", "terminal", {**_TERM, "exitCode": 3}, [{"type": "terminal/exited"}])
case(
    "term/commandExecuted-absent-fields",
    "terminal",
    _TERM,
    [{"type": "terminal/commandExecuted"}],
)
case(
    "term/commandExecuted-null-fields",
    "terminal",
    _TERM,
    [
        {
            "type": "terminal/commandExecuted",
            "commandId": None,
            "commandLine": None,
            "timestamp": None,
        }
    ],
)

# commandId matching is `===`: absent id vs explicit null must not match.
case(
    "term/finish-null-id-vs-absent-part",
    "terminal",
    {
        **_TERM,
        "content": [{"type": "command", "commandLine": "a", "output": "", "isComplete": False}],
    },
    [{"type": "terminal/commandFinished", "commandId": None, "exitCode": 9}],
)
case(
    "term/finish-object-id",
    "terminal",
    {
        **_TERM,
        "content": [
            {
                "type": "command",
                "commandId": {"a": 1},
                "commandLine": "a",
                "output": "",
                "isComplete": False,
            }
        ],
    },
    [{"type": "terminal/commandFinished", "commandId": {"a": 1}, "exitCode": 0}],
)
case(
    "term/finish-absent-both",
    "terminal",
    {
        **_TERM,
        "content": [{"type": "command", "commandLine": "a", "output": "", "isComplete": False}],
    },
    [{"type": "terminal/commandFinished", "exitCode": 7}],
)
case(
    "term/finish-no-exit-clears",
    "terminal",
    {
        **_TERM,
        "content": [
            {
                "type": "command",
                "commandId": "c1",
                "commandLine": "a",
                "output": "",
                "isComplete": False,
                "exitCode": 4,
                "durationMs": 12,
            }
        ],
    },
    [{"type": "terminal/commandFinished", "commandId": "c1"}],
)

# terminal/data: JS `+` stringifies whatever it is handed.
case(
    "term/data-number",
    "terminal",
    {**_TERM, "content": [{"type": "unclassified", "value": "x"}]},
    [{"type": "terminal/data", "data": 5}],
)
case(
    "term/data-null",
    "terminal",
    {**_TERM, "content": [{"type": "unclassified", "value": "x"}]},
    [{"type": "terminal/data", "data": None}],
)
case(
    "term/data-absent",
    "terminal",
    {**_TERM, "content": [{"type": "unclassified", "value": "x"}]},
    [{"type": "terminal/data"}],
)
case("term/data-into-empty", "terminal", _TERM, [{"type": "terminal/data", "data": "hi"}])
case(
    "term/data-tail-no-isComplete",
    "terminal",
    {**_TERM, "content": [{"type": "command", "commandId": "c", "output": "o"}]},
    [{"type": "terminal/data", "data": "!"}],
)

# ── changeset ────────────────────────────────────────────────────────────────

case(
    "cs/review-null-id-vs-absent",
    "changeset",
    {"status": "ready", "files": [{"edit": {}}, {"id": "a", "edit": {}}]},
    [{"type": "changeset/filesReviewChanged", "files": [None], "reviewed": True}],
)
case(
    "cs/review-string-not-array",
    "changeset",
    {"status": "ready", "files": [{"id": "a", "edit": {}}]},
    [{"type": "changeset/filesReviewChanged", "files": "a", "reviewed": True}],
)
case(
    "cs/review-reviewed-absent",
    "changeset",
    {"status": "ready", "files": [{"id": "a", "edit": {}, "reviewed": None}]},
    [{"type": "changeset/filesReviewChanged", "files": ["a"]}],
)
case(
    "cs/remove-null-fileId",
    "changeset",
    {"status": "ready", "files": [{"edit": 1}, {"id": "a"}]},
    [{"type": "changeset/fileRemoved", "fileId": None}],
)
case(
    "cs/remove-absent-fileId",
    "changeset",
    {"status": "ready", "files": [{"id": None}, {"id": "a"}]},
    [{"type": "changeset/fileRemoved"}],
)
case(
    "cs/remove-bool-vs-zero",
    "changeset",
    {"status": "ready", "files": [{"id": 0}]},
    [{"type": "changeset/fileRemoved", "fileId": False}],
)
case(
    "cs/fileSet-object-id",
    "changeset",
    {"status": "ready", "files": [{"id": {"k": 1}, "v": "old"}]},
    [{"type": "changeset/fileSet", "file": {"id": {"k": 1}, "v": "new"}}],
)
case(
    "cs/opStatus-null-id",
    "changeset",
    {"status": "ready", "files": [], "operations": [{"label": "x"}]},
    [{"type": "changeset/operationStatusChanged", "operationId": None, "status": "running"}],
)

# ── annotations ──────────────────────────────────────────────────────────────

case(
    "ann/removed-null-id-vs-absent",
    "annotations",
    {"annotations": [{"turnId": "t", "resource": "file:///a", "resolved": False, "entries": []}]},
    [{"type": "annotations/removed", "annotationId": None}],
)
case(
    "ann/removed-absent-id",
    "annotations",
    {"annotations": [{"id": None, "entries": []}, {"id": "real", "entries": []}]},
    [{"type": "annotations/removed"}],
)
case(
    "ann/removed-true-vs-one",
    "annotations",
    {"annotations": [{"id": 1, "entries": []}, {"id": "real", "entries": []}]},
    [{"type": "annotations/removed", "annotationId": True}],
)
case(
    "ann/set-object-id-never-matches",
    "annotations",
    {"annotations": [{"id": {"k": "v"}, "entries": [{"id": "e", "text": "keep"}]}]},
    [{"type": "annotations/set", "annotation": {"id": {"k": "v"}, "entries": []}}],
)
case(
    "ann/primitive-member-participates",
    "annotations",
    {"annotations": ["nope", {"id": "a1", "entries": []}]},
    [{"type": "annotations/removed"}],
)
case(
    "ann/entryRemoved-null-entryId",
    "annotations",
    {"annotations": [{"id": "a", "entries": [{"text": "no id"}, {"id": "e", "text": "x"}]}]},
    [{"type": "annotations/entryRemoved", "annotationId": "a", "entryId": None}],
)
case(
    "ann/updated-null-is-written",
    "annotations",
    {"annotations": [{"id": "a", "turnId": "t1", "entries": []}]},
    [{"type": "annotations/updated", "annotationId": "a", "turnId": None}],
)

# ── session (the pre-existing helpers) ───────────────────────────────────────

_SESSION = {
    "provider": "p",
    "title": "T",
    "status": 1,
    "lifecycle": "ready",
    "activeClients": [],
    "chats": [],
}

case("sess/title=null", "session", _SESSION, [{"type": "session/titleChanged", "title": None}])
case("sess/title-absent", "session", _SESSION, [{"type": "session/titleChanged"}])
case(
    "sess/activity=null",
    "session",
    {**_SESSION, "activity": "thinking"},
    [{"type": "session/activityChanged", "activity": None}],
)
case(
    "sess/defaultChat=null",
    "session",
    {**_SESSION, "defaultChat": "c"},
    [{"type": "session/defaultChatChanged", "defaultChat": None}],
)
case(
    "sess/meta=null",
    "session",
    {**_SESSION, "_meta": {"a": 1}},
    [{"type": "session/metaChanged", "_meta": None}],
)
case(
    "sess/chatRemoved-null",
    "session",
    {**_SESSION, "chats": [{"resource": None}, {"resource": "c1"}]},
    [{"type": "session/chatRemoved", "chat": None}],
)
case(
    "sess/activeClientRemoved-absent",
    "session",
    {**_SESSION, "activeClients": [{"clientId": None}, {"clientId": "c1"}]},
    [{"type": "session/activeClientRemoved"}],
)
case(
    "sess/inputNeededRemoved-bool",
    "session",
    {**_SESSION, "inputNeeded": [{"id": 1}, {"id": "x"}]},
    [{"type": "session/inputNeededRemoved", "id": True}],
)

# ── chat: strict ids, spread-of-anything answers, truthiness gates ───────────
#
# Each case pins one hazard the review found ported wrong once: `===` on
# peer-controlled ids (bool-vs-int, structurally-equal objects), the JS
# object-spread of a non-object (`{...'ab'}` has index keys, `{...42}` is
# empty), the null image of an `undefined` array element under
# JSON.stringify, and `if (action.approved)` being ToBoolean, not `is None`.

_CHAT = {"turns": [], "status": 1, "modifiedAt": "1970-01-01T00:00:00.000Z"}


def _chat_tool_call(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "activeTurn": {
            "id": "t1",
            "startedAt": "1970-01-01T00:00:00.000Z",
            "message": {"text": "hi", "origin": {"kind": "user"}},
            "responseParts": [
                {
                    "kind": "toolCall",
                    "toolCall": {
                        "toolCallId": "c1",
                        "status": status,
                        "invocationMessage": "running",
                        "toolInput": '{"arg": 1}',
                        **extra,
                    },
                }
            ],
        },
        **_CHAT,
    }


# turns.findIndex(t => t.id === action.turnId): true === 1 is false in JS,
# True == 1 is True in Python -- the no-op must survive the port.
case(
    "chat/truncated-true-vs-1",
    "chat",
    {**_CHAT, "turns": [{"id": 1, "startedAt": "1970-01-01T00:00:00.000Z"}]},
    [{"type": "chat/truncated", "turnId": True}],
)
# filter(m => m.id !== action.id): object identity, so a structurally-equal
# id parsed from a different frame removes nothing.
case(
    "chat/pendingMessageRemoved-object-id",
    "chat",
    {
        **_CHAT,
        "queuedMessages": [{"id": {"a": 1}, "message": {"text": "m", "origin": {"kind": "user"}}}],
    },
    [{"type": "chat/pendingMessageRemoved", "id": {"a": 1}}],
)
# One absent append pins the null image; a second in the SAME process would
# no-op upstream (`includes` finds the in-memory `undefined`) but append again
# here, because our state never holds the sentinel -- a documented divergence,
# pinned by a unit test rather than by this oracle. The explicit-null case has
# no such split: both sides store null and dedupe on it.
case(
    "chat/workingDirectorySet-absent-appends-null-image",
    "chat",
    _CHAT,
    [{"type": "chat/workingDirectorySet"}],
)
case(
    "chat/workingDirectorySet-null-dedupes-against-parsed-null",
    "chat",
    {**_CHAT, "workingDirectories": [None]},
    [{"type": "chat/workingDirectorySet", "directory": None}],
)
# {...(part.request.answers ?? {}), ...(action.answers ?? {})} over a string
# yields index keys; over a number it yields {} and the answers key drops.
case(
    "chat/inputCompleted-string-answers",
    "chat",
    {
        **_CHAT,
        "activeTurn": {
            "id": "t1",
            "startedAt": "1970-01-01T00:00:00.000Z",
            "message": {"text": "hi", "origin": {"kind": "user"}},
            "responseParts": [{"kind": "inputRequest", "request": {"id": "r1", "questions": []}}],
        },
    },
    [{"type": "chat/inputCompleted", "requestId": "r1", "response": "accept", "answers": "ab"}],
)
case(
    "chat/inputCompleted-number-answers",
    "chat",
    {
        **_CHAT,
        "activeTurn": {
            "id": "t1",
            "startedAt": "1970-01-01T00:00:00.000Z",
            "message": {"text": "hi", "origin": {"kind": "user"}},
            "responseParts": [{"kind": "inputRequest", "request": {"id": "r1", "questions": []}}],
        },
    },
    [{"type": "chat/inputCompleted", "requestId": "r1", "response": "accept", "answers": 42}],
)
# `if (action.approved)` is ToBoolean: {} approves, and the call runs.
case(
    "chat/toolCallConfirmed-approved-empty-object",
    "chat",
    _chat_tool_call("pending-confirmation"),
    [
        {
            "type": "chat/toolCallConfirmed",
            "turnId": "t1",
            "toolCallId": "c1",
            "approved": {},
            "confirmed": "user",
        }
    ],
)
# refineToolCallContributor: `if (!next)` keeps the existing contributor for
# every falsy replacement, '' included -- not only for null/absent.
case(
    "chat/toolCallReady-empty-contributor",
    "chat",
    _chat_tool_call("streaming", contributor={"kind": "mcpServer", "serverName": "srv"}),
    [
        {
            "type": "chat/toolCallReady",
            "turnId": "t1",
            "toolCallId": "c1",
            "contributor": "",
            "confirmed": "not-needed",
        }
    ],
)

if __name__ == "__main__":
    for entry in CASES:
        sys.stdout.write(json.dumps(entry) + "\n")
