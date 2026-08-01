"""Adversarial cases the fixture corpus cannot express.

Every case here targets one of the two defect classes the audit named:
undefined-vs-null on an unconditional spread, and `===` vs `==` on an id lookup.
"""

from __future__ import annotations

import json
import sys

CASES: list[dict] = []


def case(name: str, reducer: str, initial, actions: list[dict]) -> None:
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

if __name__ == "__main__":
    for entry in CASES:
        sys.stdout.write(json.dumps(entry) + "\n")
