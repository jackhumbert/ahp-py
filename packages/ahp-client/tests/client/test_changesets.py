"""The changeset surface: the catalogue, the diffs, review and operations.

`docs/plan.md` §1.3 scopes this one as read-mostly -- one client-dispatchable
action out of eight -- so most of what is asserted here is that a *view* reads
the right key, and the rest is the four checks a caller cannot make from the
params alone: the content ref rather than the file URI, the review capability
read live off the catalogue entry, `operationId` and `target.kind` against the
changeset's own declaration, and a `confirmation` the client MUST show.

Driven off `ahp_client.testing.fake_host` so the suite stays independent
of the sibling host; the last block drives the sibling for real, and says in its
own docstring why that is not independent evidence.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
from typing import Any

import pytest
from ahp_protocol.conformance.corpus import CORPUS_ROOT

from ahp_client.api import connect, text_range
from ahp_client.api.changesets import (
    ChangesetFile,
    ChangesetInfo,
    FileSide,
    _text_range,
)
from ahp_client.client import actions
from ahp_client.client.commands import CommandsMixin
from ahp_client.client.errors import AhpClientError, InvalidArgument
from ahp_client.testing import FakeHost

from ._sibling import requires_sibling_host

CHAT = "ahp-chat://c/s"
CHANGESET = "ahp-changeset:/one"

#: The vendored schema, not a transcription of it.
SCHEMA = json.loads((CORPUS_ROOT / "schema" / "commands.schema.json").read_text(encoding="utf-8"))

_EDIT = {
    "id": "file:///work/main.py",
    "edit": {
        "before": {
            "uri": "file:///work/main.py",
            "content": {"uri": "ahp-changeset-content:/aaa", "sizeHint": 13},
        },
        "after": {
            "uri": "file:///work/main.py",
            "content": {"uri": "ahp-changeset-content:/bbb", "sizeHint": 26},
        },
        "diff": {"added": 1, "removed": 0},
    },
}

_STAGE = {
    "id": "stage",
    "label": "Stage all",
    "scopes": ["changeset", "resource"],
    "status": "idle",
}
_ANNOTATE = {"id": "annotate", "label": "Explain", "scopes": ["range"], "status": "idle"}
_REVERT = {
    "id": "revert",
    "label": "Revert",
    "scopes": ["resource"],
    "status": "idle",
    "confirmation": "Discard the agent's edits to this file?",
}


def _host(
    *,
    catalogue: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    content: dict[str, dict[str, Any]] | None = None,
) -> FakeHost:
    """A host with one session, one changeset channel, and a content store."""
    host = FakeHost(agents=[{"provider": "echo", "displayName": "Echo"}])
    entries = (
        catalogue
        if catalogue is not None
        else [
            {
                "label": "Uncommitted",
                "uriTemplate": CHANGESET,
                "changeKind": "uncommitted",
                "capabilities": {"review": {}},
            }
        ]
    )
    session_state: dict[str, Any] = {
        "lifecycle": "ready",
        "defaultChat": CHAT,
        "interactivity": "full",
        "changesets": entries,
    }
    changeset_state = state if state is not None else {"status": "ready", "files": [_EDIT]}
    store = content or {}

    def subscribe(params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        if channel.startswith("ahp-changeset:"):
            body: Any = changeset_state
        elif channel.startswith("ahp-chat:"):
            body = {"turns": [], "activeTurn": None}
        elif channel == "ahp-root://":
            body = host.root_state
        else:
            body = session_state
        return {"snapshot": {"resource": channel, "state": body, "fromSeq": host._server_seq}}

    def resource_read(params: dict[str, Any]) -> dict[str, Any]:
        answer = store.get(str(params.get("uri")))
        if answer is None:
            raise AssertionError(f"resourceRead for {params.get('uri')!r}, which is not content")
        return answer

    host.on("initialize", lambda p: _initialize(host, p))
    host.on("ping", lambda _p: {})
    host.on("listSessions", lambda _p: {"items": []})
    host.on("subscribe", subscribe)
    host.on("createSession", lambda _p: {})
    host.on("disposeSession", lambda _p: {})
    host.on("resourceRead", resource_read)
    host.on("invokeChangesetOperation", lambda _p: {})
    host.session_state = session_state  # type: ignore[attr-defined]
    return host


def _initialize(host: FakeHost, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": host.protocol_version,
        "serverSeq": host._server_seq,
        "snapshots": [
            {"resource": uri, "state": host.root_state, "fromSeq": host._server_seq}
            for uri in params.get("initialSubscriptions") or []
            if uri == "ahp-root://"
        ],
    }


def _sent(host: FakeHost, method: str) -> list[dict[str, Any]]:
    return [m["params"] for m in host.received if m.get("method") == method]


def _dispatched(host: FakeHost, action_type: str) -> list[dict[str, Any]]:
    return [
        m["params"]["action"]
        for m in host.received
        if m.get("method") == "dispatchAction" and m["params"]["action"]["type"] == action_type
    ]


async def _settle(predicate: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


# ── the catalogue ────────────────────────────────────────────────────────────


async def test_the_catalogue_is_readable_without_subscribing_to_anything() -> None:
    """ "Just enough to render a chip or list row without subscribing.\""""
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        (entry,) = session.changesets()
        assert entry.label == "Uncommitted"
        assert entry.change_kind == "uncommitted"
        assert entry.uri_template == CHANGESET
        # Nothing was subscribed to read that.
        assert [p["channel"] for p in _sent(host, "subscribe")] == [session.uri]
    await host.stop()


async def test_review_is_a_presence_flag_and_an_empty_object_advertises_it() -> None:
    """The `capabilities.multipleChats` trap again: `{"review": {}}` means
    supported and `{}` is falsy in Python, so a truthiness test reads every
    reviewable changeset as non-reviewable."""
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        entry = session.changesets()[0]
        assert entry.capabilities["review"] == {}
        assert entry.reviewable is True
    await host.stop()


async def test_a_changeset_advertising_nothing_is_not_reviewable() -> None:
    host = _host(catalogue=[{"label": "c", "uriTemplate": CHANGESET, "changeKind": "session"}])
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        assert session.changesets()[0].reviewable is False
    await host.stop()


# ── template expansion ───────────────────────────────────────────────────────


async def test_a_variable_free_template_is_itself_the_channel() -> None:
    """Which is the shape every real host mints, and the reason the common case
    passes no variables at all."""
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        entry = session.changesets()[0]
        assert entry.variables == frozenset()
        assert entry.expand() == CHANGESET
        changeset = await session.open_changeset(entry)
        assert changeset.uri == CHANGESET
        assert [p["channel"] for p in _sent(host, "subscribe")][-1] == CHANGESET
    await host.stop()


async def test_a_turn_template_is_expanded_and_percent_encoded() -> None:
    """RFC 6570 simple string expansion encodes to the unreserved set; a turn id
    carrying a `/` would otherwise produce a channel the host never registered."""
    host = _host(
        catalogue=[
            {"label": "This turn", "uriTemplate": "ahp-changeset:/t/{turnId}", "changeKind": "turn"}
        ]
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        entry = session.changesets()[0]
        assert entry.variables == {"turnId"}
        assert entry.expand(turn_id="a/b") == "ahp-changeset:/t/a%2Fb"
        changeset = await session.open_changeset(entry, turn_id="turn-1")
        assert changeset.uri == "ahp-changeset:/t/turn-1"
    await host.stop()


async def test_a_template_variable_this_version_does_not_define_is_unopenable() -> None:
    """ "Any other variable name MUST be ignored by clients (there is no
    protocol-defined way to obtain values for unknown variables)." A wrapper
    that expanded it anyway would subscribe to a URI nobody registered."""
    host = _host(
        catalogue=[
            {"label": "c", "uriTemplate": "ahp-changeset:/{branchName}", "changeKind": "branch"}
        ]
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        entry = session.changesets()[0]
        assert entry.openable is False
        with pytest.raises(AhpClientError, match="branchName"):
            entry.expand()
    await host.stop()


async def test_a_turn_comparison_needs_both_halves_of_its_pair() -> None:
    """ "Both variables MUST be present.\""""
    host = _host(
        catalogue=[
            {
                "label": "c",
                "uriTemplate": "ahp-changeset:/{originalTurnId}",
                "changeKind": "compare-turns",
            }
        ]
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        entry = session.changesets()[0]
        assert entry.openable is False
        with pytest.raises(AhpClientError, match="modifiedTurnId"):
            entry.expand(original_turn_id="a", modified_turn_id="b")
    await host.stop()


async def test_a_missing_value_is_named_rather_than_expanded_to_the_literal() -> None:
    host = _host(
        catalogue=[{"label": "c", "uriTemplate": "ahp-changeset:/{turnId}", "changeKind": "turn"}]
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        with pytest.raises(AhpClientError, match="turnId"):
            session.changesets()[0].expand()
    await host.stop()


# ── the file list ────────────────────────────────────────────────────────────


async def _open(host: FakeHost) -> Any:
    client_ctx = connect(transport=host.transport())
    client = await client_ctx
    session = await client.create_session(provider="echo")
    return client, await session.open_changeset(session.changesets()[0])


async def test_the_three_diff_shapes_are_derived_from_before_and_after() -> None:
    """The protocol carries no discriminator: "supports creates (only `after`),
    deletes (only `before`), renames/moves (different `uri`), and edits\"."""
    created = {"id": "file:///work/new.py", "edit": {"after": {"uri": "file:///work/new.py"}}}
    deleted = {"id": "file:///work/old.py", "edit": {"before": {"uri": "file:///work/old.py"}}}
    renamed = {
        "id": "file:///work/b.py",
        "edit": {"before": {"uri": "file:///work/a.py"}, "after": {"uri": "file:///work/b.py"}},
    }
    host = _host(state={"status": "ready", "files": [_EDIT, created, deleted, renamed]})
    await host.start()
    client, changeset = await _open(host)
    assert [f.change for f in changeset.files()] == ["modified", "created", "deleted", "renamed"]
    await client.aclose()
    await host.stop()


async def test_the_line_counts_read_added_and_removed() -> None:
    """`FileEdit.diff` spells them `added`/`removed`; `SessionSummary.changes`
    spells the same quantities `additions`/`deletions`. Reading the summary's
    names off a file renders every file as +0 -0, which is a defect the sibling
    host shipped in the other direction."""
    host = _host()
    await host.start()
    client, changeset = await _open(host)
    file = changeset.files()[0]
    assert (file.added, file.removed) == (1, 0)
    await client.aclose()
    await host.stop()


async def test_a_missing_reviewed_flag_is_not_reviewed() -> None:
    """ "Absent is equivalent to `false` -- clients MUST treat a missing value as
    not-yet-reviewed.\""""
    host = _host()
    await host.start()
    client, changeset = await _open(host)
    assert "reviewed" not in changeset.files()[0].raw
    assert changeset.files()[0].reviewed is False
    await client.aclose()
    await host.stop()


# ── content ──────────────────────────────────────────────────────────────────


async def test_content_is_fetched_by_content_ref_never_by_the_file_uri() -> None:
    """The part a hand-rolled caller gets wrong. `edit.after.uri` names the
    file; the bytes live behind `edit.after.content`, a `ContentRef` into a
    store the host owns -- and a changeset renders on a host that exposes no
    filesystem at all, so the file URI may have no answer whatsoever. The fake
    host asserts on any read that is not a content ref."""
    host = _host(
        content={
            "ahp-changeset-content:/aaa": {"data": "print('one')\n", "encoding": "utf-8"},
            "ahp-changeset-content:/bbb": {"data": "print('two')\n", "encoding": "utf-8"},
        }
    )
    await host.start()
    client, changeset = await _open(host)
    file = changeset.files()[0]
    assert await changeset.read(file.before) == b"print('one')\n"
    assert await changeset.read_text(file.after) == "print('two')\n"
    assert [p["uri"] for p in _sent(host, "resourceRead")] == [
        "ahp-changeset-content:/aaa",
        "ahp-changeset-content:/bbb",
    ]
    await client.aclose()
    await host.stop()


async def test_binary_content_arrives_base64_and_is_decoded() -> None:
    """ "Binary content MUST use `base64`; text content MAY use `utf-8`", so the
    encoding is the receiver's choice and a caller who assumes text gets a
    base64 blob the first time a changeset touches a PNG."""
    host = _host(
        content={
            "ahp-changeset-content:/bbb": {
                "data": base64.b64encode(b"\x89PNG\x00").decode(),
                "encoding": "base64",
                "contentType": "image/png",
            }
        }
    )
    await host.start()
    client, changeset = await _open(host)
    assert await changeset.read(changeset.files()[0].after) == b"\x89PNG\x00"
    await client.aclose()
    await host.stop()


async def test_the_absent_side_of_a_creation_reads_as_nothing() -> None:
    """A creation has no `before` and a deletion no `after`, so a diff renderer
    asks for both and one of them is legitimately empty."""
    created = {"id": "file:///work/new.py", "edit": {"after": {"uri": "file:///work/new.py"}}}
    host = _host(state={"status": "ready", "files": [created]})
    await host.start()
    client, changeset = await _open(host)
    assert await changeset.read(changeset.files()[0].before) is None
    assert _sent(host, "resourceRead") == []
    await client.aclose()
    await host.stop()


# ── review ───────────────────────────────────────────────────────────────────


async def test_review_dispatches_the_one_client_dispatchable_action() -> None:
    host = _host()
    await host.start()
    client, changeset = await _open(host)
    changeset.mark_reviewed(["file:///work/main.py"])
    await _settle(lambda: _dispatched(host, "changeset/filesReviewChanged"))
    action = _dispatched(host, "changeset/filesReviewChanged")[-1]
    assert action == {
        "type": "changeset/filesReviewChanged",
        "files": ["file:///work/main.py"],
        "reviewed": True,
    }
    await client.aclose()
    await host.stop()


async def test_review_is_refused_when_the_changeset_never_advertised_it() -> None:
    """ "Requires the changeset to advertise `capabilities.review`." Sending it
    anyway earns a rejected echo that reverts the optimistic tick a moment after
    the box appeared to move."""
    host = _host(catalogue=[{"label": "c", "uriTemplate": CHANGESET, "changeKind": "session"}])
    await host.start()
    client, changeset = await _open(host)
    with pytest.raises(AhpClientError, match=re.escape("capabilities.review")):
        changeset.mark_reviewed(["file:///work/main.py"])
    assert _dispatched(host, "changeset/filesReviewChanged") == []
    await client.aclose()
    await host.stop()


async def test_the_review_gate_is_re_read_rather_than_remembered() -> None:
    """The capability lives on the **catalogue entry**, not on the changeset's
    own state, and the host validates against the *current* entry -- so a
    changeset that stops being reviewable has to stop offering review here in
    the same breath. Caching it at open time is how a client keeps rendering
    checkboxes the host then refuses."""
    host = _host()
    await host.start()
    client, changeset = await _open(host)
    changeset.mark_reviewed(["file:///work/main.py"])

    await host.push(
        _sent(host, "createSession")[-1]["channel"],
        {
            "type": "session/changesetsChanged",
            "changesets": [{"label": "c", "uriTemplate": CHANGESET, "changeKind": "uncommitted"}],
        },
    )
    await _settle(lambda: changeset.info is not None and not changeset.info.reviewable)
    with pytest.raises(AhpClientError, match=re.escape("capabilities.review")):
        changeset.mark_reviewed(["file:///work/main.py"])
    await client.aclose()
    await host.stop()


async def test_a_changeset_with_no_catalogue_entry_says_so() -> None:
    """Opened by bare URI on a session whose catalogue does not list it: there
    is nothing advertising review, and guessing "yes" would send an action the
    host rejects."""
    host = _host(catalogue=[])
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        changeset = await session.open_changeset(CHANGESET)
        assert changeset.info is None
        with pytest.raises(AhpClientError, match="no catalogue entry"):
            changeset.mark_reviewed(["file:///work/main.py"])
    await host.stop()


def test_an_empty_review_batch_is_refused_rather_than_sent() -> None:
    """ "Ids that do not match a file currently present in the changeset are
    ignored; if none match, the action is a no-op." So an empty list is accepted,
    numbered and broadcast, and changes nothing anywhere -- the same silent
    shape as the missing `turnId` this module exists for."""
    with pytest.raises(ValueError, match="no-op"):
        actions.files_review_changed([], True)


def test_an_empty_file_id_is_refused() -> None:
    """`ids.has(f.id)` is SameValueZero, so `""` is a real key that matches a
    file whose id is `""` -- silently, and nothing else."""
    with pytest.raises(ValueError, match="file id"):
        actions.files_review_changed(["a", ""], True)


# ── operations ───────────────────────────────────────────────────────────────


async def test_an_undeclared_operation_never_reaches_the_wire() -> None:
    """ "The server validates that `operationId` exists in the changeset's
    current `operations` list ... Invalid combinations result in a JSON-RPC
    error." Learning that from a -32602 is what a typed API should spare you --
    and the declaration *moves*, because a host recomputes it."""
    host = _host(state={"status": "ready", "files": [_EDIT], "operations": [_STAGE]})
    await host.start()
    client, changeset = await _open(host)
    with pytest.raises(AhpClientError, match=r"\['stage'\]"):
        await changeset.invoke("commit")
    assert _sent(host, "invokeChangesetOperation") == []
    await client.aclose()
    await host.stop()


async def test_a_changeset_declaring_no_operations_says_so_plainly() -> None:
    host = _host()
    await host.start()
    client, changeset = await _open(host)
    with pytest.raises(AhpClientError, match="declares none at all"):
        await changeset.invoke("stage")
    await client.aclose()
    await host.stop()


async def test_a_scope_the_operation_did_not_declare_is_refused() -> None:
    """ "The `kind` MUST match one of the operation's declared `scopes`." `stage`
    here is changeset- and resource-scoped, so a range target is not a thing it
    can do."""
    host = _host(state={"status": "ready", "files": [_EDIT], "operations": [_STAGE]})
    await host.start()
    client, changeset = await _open(host)
    with pytest.raises(AhpClientError, match="not 'range'"):
        await changeset.invoke(
            "stage", resource="file:///work/main.py", range=text_range(0, 0, 1, 0)
        )
    assert _sent(host, "invokeChangesetOperation") == []
    await client.aclose()
    await host.stop()


async def test_a_changeset_scoped_invocation_omits_the_target_entirely() -> None:
    """ "Required iff the chosen scope is `resource` or `range`. Omit for
    changeset-scoped operations.\""""
    host = _host(state={"status": "ready", "files": [_EDIT], "operations": [_STAGE]})
    await host.start()
    client, changeset = await _open(host)
    await changeset.invoke("stage")
    params = _sent(host, "invokeChangesetOperation")[-1]
    assert params["channel"] == CHANGESET
    assert params["operationId"] == "stage"
    assert "target" not in params
    required = SCHEMA["$defs"]["InvokeChangesetOperationParams"]["required"]
    assert [key for key in required if key not in params] == []
    await client.aclose()
    await host.stop()


async def test_a_range_operation_carries_its_range() -> None:
    host = _host(state={"status": "ready", "files": [_EDIT], "operations": [_ANNOTATE]})
    await host.start()
    client, changeset = await _open(host)
    await changeset.invoke(
        "annotate",
        resource="file:///work/main.py",
        range=text_range(3, 0, 7, 12),
        side="after",
    )
    target = _sent(host, "invokeChangesetOperation")[-1]["target"]
    assert target == {
        "kind": "range",
        "resource": "file:///work/main.py",
        "side": "after",
        "range": {"start": {"line": 3, "character": 0}, "end": {"line": 7, "character": 12}},
    }
    await client.aclose()
    await host.stop()


async def test_a_range_scoped_operation_with_no_resource_is_refused() -> None:
    """A range target names a range *within a single file*, and the resource is
    that file."""
    host = _host(state={"status": "ready", "files": [_EDIT], "operations": [_ANNOTATE]})
    await host.start()
    client, changeset = await _open(host)
    with pytest.raises(AhpClientError, match="needs the resource"):
        await changeset.invoke("annotate", range=text_range(0, 0, 1, 0))
    await client.aclose()
    await host.stop()


async def test_a_malformed_range_is_caught_before_it_is_sent() -> None:
    host = _host(state={"status": "ready", "files": [_EDIT], "operations": [_ANNOTATE]})
    await host.start()
    client, changeset = await _open(host)
    with pytest.raises(AhpClientError, match="character"):
        await changeset.invoke(
            "annotate", resource="file:///work/main.py", range={"start": {"line": 0}, "end": {}}
        )
    assert _sent(host, "invokeChangesetOperation") == []
    await client.aclose()
    await host.stop()


async def test_a_destructive_operation_is_not_sent_unconfirmed() -> None:
    """ "When present, the client MUST display this message to the user ... and
    only invoke the operation after the user accepts. The presence of this field
    also signals that the operation is destructive." A library that sent it
    anyway would make every caller quietly violate that MUST -- and the sibling
    host's `Revert` really does discard the agent's edits."""
    host = _host(state={"status": "ready", "files": [_EDIT], "operations": [_REVERT]})
    await host.start()
    client, changeset = await _open(host)
    operation = changeset.operation("revert")
    assert operation is not None
    assert operation.destructive is True
    assert operation.confirmation_text == "Discard the agent's edits to this file?"

    with pytest.raises(AhpClientError, match="MUST show"):
        await changeset.invoke("revert", resource="file:///work/main.py")
    assert _sent(host, "invokeChangesetOperation") == []

    await changeset.invoke("revert", resource="file:///work/main.py", confirmed=True)
    assert len(_sent(host, "invokeChangesetOperation")) == 1
    await client.aclose()
    await host.stop()


async def test_a_markdown_confirmation_keeps_which_it_is() -> None:
    """`StringOrMarkdown`: "a plain `string` is rendered as-is (no Markdown
    processing)", so flattening the two would lose how to render it."""
    prompt = {"markdown": "**Discard** these edits?"}
    host = _host(
        state={
            "status": "ready",
            "files": [_EDIT],
            "operations": [{**_REVERT, "confirmation": prompt}],
        }
    )
    await host.start()
    client, changeset = await _open(host)
    operation = changeset.operation("revert")
    assert operation is not None
    assert operation.confirmation == prompt
    assert operation.confirmation_text == "**Discard** these edits?"
    await client.aclose()
    await host.stop()


async def test_a_failed_operation_carries_its_error_rather_than_raising() -> None:
    """ "Its progress and outcome are reflected back into changeset state so that
    every subscriber observes a consistent view." The invocation succeeded; the
    operation failed afterwards, and only the action stream says so."""
    host = _host(state={"status": "ready", "files": [_EDIT], "operations": [_STAGE]})
    await host.start()
    client, changeset = await _open(host)
    assert changeset.operation("stage") is not None
    await host.push(
        CHANGESET,
        {"type": "changeset/operationStatusChanged", "operationId": "stage", "status": "running"},
    )
    await _settle(lambda: (op := changeset.operation("stage")) is not None and op.running)
    await host.push(
        CHANGESET,
        {
            "type": "changeset/operationStatusChanged",
            "operationId": "stage",
            "status": "error",
            "error": {"errorType": "GitError", "message": "nothing to stage"},
        },
    )
    await _settle(lambda: (op := changeset.operation("stage")) is not None and op.failed)
    operation = changeset.operation("stage")
    assert operation is not None
    assert operation.error == {"errorType": "GitError", "message": "nothing to stage"}
    await client.aclose()
    await host.stop()


# ── watching ─────────────────────────────────────────────────────────────────


async def test_changes_wakes_on_this_channel_and_not_another() -> None:
    host = _host()
    await host.start()
    client, changeset = await _open(host)
    seen: list[int] = []

    async def watch() -> None:
        async for view in changeset.changes():
            seen.append(len(view.files()))
            if len(seen) == 1:
                return

    task = asyncio.get_running_loop().create_task(watch())
    await asyncio.sleep(0.05)
    await host.push(CHAT, {"type": "chat/turnStarted", "turnId": "t"})
    await asyncio.sleep(0.05)
    assert seen == []
    await host.push(
        CHANGESET,
        {"type": "changeset/fileSet", "file": {"id": "file:///work/two.py", "edit": {}}},
    )
    await asyncio.wait_for(task, 2.0)
    assert seen == [2]
    await client.aclose()
    await host.stop()


async def test_waiting_for_a_computing_changeset_returns_when_it_settles() -> None:
    """The host registers the channel in `computing` and returns to it on every
    refresh, so a caller that subscribes right after a turn otherwise reads an
    empty file list and concludes the agent changed nothing."""
    host = _host(state={"status": "computing", "files": []})
    await host.start()
    client, changeset = await _open(host)
    assert changeset.status == "computing"

    waiting = asyncio.get_running_loop().create_task(changeset.wait_until_ready(timeout=2.0))
    await asyncio.sleep(0.05)
    await host.push(CHANGESET, {"type": "changeset/contentChanged", "files": [_EDIT]})
    await host.push(CHANGESET, {"type": "changeset/statusChanged", "status": "ready"})
    settled = await asyncio.wait_for(waiting, 2.0)
    assert settled.ready
    assert len(settled.files()) == 1
    await client.aclose()
    await host.stop()


async def test_waiting_ends_on_an_error_status_too() -> None:
    """The wait is for the computation to settle, and `error` is settled."""
    host = _host(state={"status": "computing", "files": []})
    await host.start()
    client, changeset = await _open(host)
    waiting = asyncio.get_running_loop().create_task(changeset.wait_until_ready(timeout=2.0))
    await asyncio.sleep(0.05)
    await host.push(
        CHANGESET,
        {
            "type": "changeset/statusChanged",
            "status": "error",
            "error": {"errorType": "GitError", "message": "no such ref"},
        },
    )
    settled = await asyncio.wait_for(waiting, 2.0)
    assert settled.failed
    assert settled.error == {"errorType": "GitError", "message": "no such ref"}
    await client.aclose()
    await host.stop()


# ── interop with the sibling host ────────────────────────────────────────────

# A marker, not a module-level `importorskip`: the latter runs at import time
# and skipped every pure unit test below this line whenever the sibling was
# absent -- ~50 offline tests silently not running.
_needs_sibling = requires_sibling_host


@_needs_sibling
async def test_a_real_changeset_against_the_sibling_host(tmp_path: Any) -> None:
    """**Not independent evidence** -- both peers share the reducers -- but it is
    the only thing that exercises the content store, the review gate and the
    operation registry as one host really implements them.

    Driven against `DemoWorkspace`, which makes real edits in a real git repo, so
    the `before` bytes asserted below are the committed ones and no longer exist
    on disk by the time they are read.
    """
    import contextlib
    import subprocess

    from ahp_host.core import Host, LoopbackSingleUserPolicy
    from ahp_host.provider import EchoProvider
    from ahp_host.provider.demo_workspace import DemoWorkspace
    from ahp_protocol.transport import memory_pair

    root = tmp_path / "work"
    (root / "src" / "greeter").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "greeter" / "core.py").write_text('GREETING = "hello"\nRETRIES = 1\n')
    (root / "config.json").write_text('{"retries": 1, "verbose": false}\n')
    (root / "docs" / "notes.md").write_text("# Notes\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base"],
        cwd=root,
        check=True,
    )

    invoked: list[tuple[str, str, Any]] = []

    async def handler(changeset_uri: str, operation_id: str, target: Any) -> None:
        invoked.append((changeset_uri, operation_id, target))

    host = Host(EchoProvider(workspace=DemoWorkspace(root)), LoopbackSingleUserPolicy())
    host.register_operation("ahs-stage", handler)
    client_side, host_side = memory_pair()
    served = asyncio.get_running_loop().create_task(host.serve(host_side))
    try:
        async with connect(transport=client_side) as client:
            session = await client.create_session(provider="echo", cwd=str(root))
            await session.prompt("say howdy", idle_timeout=15.0)
            await _settle(lambda: bool(session.changesets()))

            entry = next(e for e in session.changesets() if e.change_kind == "uncommitted")
            assert entry.reviewable, "the demo advertises review"
            changeset = await session.open_changeset(entry)
            await changeset.wait_until_ready(timeout=10.0)

            core = next(f for f in changeset.files() if f.id.endswith("core.py"))
            # The committed bytes, which the working tree no longer holds.
            assert await changeset.read_text(core.before) == 'GREETING = "hello"\nRETRIES = 1\n'
            assert "howdy" in (await changeset.read_text(core.after) or "")
            assert (root / "src" / "greeter" / "core.py").read_text() != (
                await changeset.read_text(core.before)
            )

            deleted = next(f for f in changeset.files() if f.id.endswith("notes.md"))
            assert deleted.change == "deleted"
            assert await changeset.read(deleted.after) is None

            changeset.mark_reviewed([core.id])
            await _settle(lambda: (f := changeset.file(core.id)) is not None and f.reviewed)

            stage = changeset.operation("ahs-stage")
            assert stage is not None
            assert "changeset" in stage.scopes
            await changeset.invoke("ahs-stage")
            assert [op for _, op, _ in invoked] == ["ahs-stage"]

            with pytest.raises(AhpClientError, match="declares no operation"):
                await changeset.invoke("ahs-commit")
    finally:
        await host.aclose()
        served.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await served


# ── what the reviews caught ──────────────────────────────────────────────────


def test_every_brace_group_is_seen_not_just_the_ones_we_can_expand() -> None:
    """The regex matched `{name}` only, so an operator, a dotted name or a comma
    list was invisible: `variables` empty, `openable` True, and `expand`
    returning the template **with its braces** -- a channel string the host never
    registered, which subscribes fine and then never publishes anything. That is
    the silent freeze invariant 1 exists to prevent, through the derived-URI door
    rather than the scheme door.
    """
    for template in (
        "ahp-changeset:/x{+turnId}",  # reserved expansion of a variable we DO define
        "ahp-changeset:/x{?turnId}",
        "ahp-changeset:/x{/turnId}",
        "ahp-changeset:/x{turn.id}",
        "ahp-changeset:/x{originalTurnId,modifiedTurnId}",  # a legal spelling of the pair
        "ahp-changeset:/x{unknownThing}",
    ):
        entry = ChangesetInfo({"uriTemplate": template})
        assert entry.openable is False, template
        with pytest.raises(AhpClientError):
            entry.expand(turn_id="t1")

    plain = ChangesetInfo({"uriTemplate": "ahp-changeset:/x/{turnId}"})
    assert plain.openable is True
    assert plain.expand(turn_id="t/2") == "ahp-changeset:/x/t%2F2"


def test_a_value_with_no_slot_is_refused_rather_than_dropped() -> None:
    """Every other branch of `expand` is a refusal; silently discarding a
    supplied value opens the session-wide changeset while the caller believes
    they asked for one turn's."""
    entry = ChangesetInfo({"uriTemplate": "ahp-changeset:/session-wide"})
    with pytest.raises(InvalidArgument, match=r"no \['turnId'\]"):
        entry.expand(turn_id="turn-42")
    assert entry.expand() == "ahp-changeset:/session-wide"


def test_an_expanded_uri_still_finds_its_catalogue_entry() -> None:
    """`open_changeset(uri)` is the escape hatch the spec's "subscribe to what
    comes back" advice relies on. Matching the entry by `uriTemplate` alone made
    it second-class: review was refused with an error telling the caller to open
    it from `Session.changesets()`, which is what they did."""
    entry = ChangesetInfo({"uriTemplate": "ahp-changeset:/turn/{turnId}"})
    assert entry.matches("ahp-changeset:/turn/t1") is True
    assert entry.matches("ahp-changeset:/turn/t1/extra") is False
    assert entry.matches("ahp-changeset:/other/t1") is False


def test_an_in_place_edit_is_not_reported_as_a_creation() -> None:
    """ "The file state before the edit. Absent for file creations **or for
    in-place file edits**." A file with two removed lines is not a creation, and
    `read(before)` returns None for it -- so the UI renders "new file, no diff
    available" for an edit. `diff.removed` is the only corroboration the protocol
    offers, so it is used."""
    in_place = ChangesetFile(
        {
            "id": "file:///work/a.py",
            "edit": {
                "after": {"uri": "file:///work/a.py", "content": {"uri": "c"}},
                "diff": {"added": 2, "removed": 2},
            },
        }
    )
    assert in_place.change == "modified"

    created = ChangesetFile(
        {
            "id": "file:///work/new.py",
            "edit": {
                "after": {"uri": "file:///work/new.py", "content": {"uri": "c"}},
                "diff": {"added": 9, "removed": 0},
            },
        }
    )
    assert created.change == "created"
    # Neither side is not a shape the schema defines, and guessing at it is how
    # the two cases above got conflated in the first place.
    assert ChangesetFile({"id": "x", "edit": {}}).change == "unknown"


async def test_target_side_is_checked_against_its_closed_enum() -> None:
    """`ChangesetOperationTarget.side` is `before|after` on both branches, and
    **nothing else in the stack checks it** -- the sibling host validates `kind`,
    `resource` and both range positions and never reads `side`. So an arbitrary
    string reaches a handler that branches on it and reads it as neither side,
    which is the defect `_text_range` refuses to leave open on the field beside
    it."""
    host = _host(state={"status": "ready", "files": [_EDIT], "operations": [_STAGE]})
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        changeset = await session.open_changeset(session.changesets()[0])
        with pytest.raises(InvalidArgument, match=r"target\.side"):
            await changeset.invoke("stage", resource="file:///work/main.py", side="ORIGINAL")  # type: ignore[arg-type]
        assert _sent(host, "invokeChangesetOperation") == []
        await changeset.invoke("stage", resource="file:///work/main.py", side="after")
        assert _sent(host, "invokeChangesetOperation")[-1]["target"]["side"] == "after"
    await host.stop()


def test_the_command_names_the_operation_id_it_requires() -> None:
    """`operationId` is required by `InvokeChangesetOperationParams`, and a
    `**extra`-only signature type-checks clean under `mypy --strict` while
    omitting it. That is the shape `actions.py` was extracted to stop."""
    signature = inspect.signature(CommandsMixin.invoke_changeset_operation)
    parameter = signature.parameters["operation_id"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


async def test_a_settled_status_is_not_the_same_as_no_status() -> None:
    """`status != "computing"` returned instantly for the empty string, which is
    what a dropped, disposed or never-registered channel reads as -- blessing
    exactly the empty file list this method exists to stop a caller believing."""
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        changeset = await session.open_changeset(session.changesets()[0])
        assert changeset.live is True
        await changeset.aclose()
        assert changeset.live is False
        with pytest.raises(AhpClientError, match="not subscribed"):
            await changeset.wait_until_ready(timeout=0.1)
    await host.stop()


async def test_a_second_handle_survives_the_first_ones_close() -> None:
    """`open_changeset` mints a fresh object per call and does not memoise, so
    two handles on one changeset is the ordinary case. An unrefcounted
    unsubscribe left the survivor with no files, an instant `wait_until_ready`
    and dispatches into a channel this client no longer received."""
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        entry = session.changesets()[0]
        first = await session.open_changeset(entry)
        second = await session.open_changeset(entry)
        await second.aclose()
        assert first.live is True
        assert len(first.files()) == 1
        assert await asyncio.wait_for(first.wait_until_ready(timeout=1), 2) is first
    await host.stop()


async def test_changes_wakes_on_the_catalogue_and_ends_when_closed() -> None:
    """`info` is deliberately live, and the case it is live for -- a changeset
    that stops being reviewable mid-session -- moves nothing on the changeset
    channel at all. Waking only there made the one loop this API offers unable to
    observe the one thing the re-read was built for."""
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        changeset = await session.open_changeset(session.changesets()[0])
        woke: list[bool] = []

        async def watch() -> None:
            async for view in changeset.changes():
                woke.append(view.info is not None and view.info.reviewable)

        watching = asyncio.create_task(watch())
        await asyncio.sleep(0.05)
        await host.push(
            session.uri,
            {
                "type": "session/changesetsChanged",
                "changesets": [
                    {"label": "Uncommitted", "uriTemplate": CHANGESET, "capabilities": {}}
                ],
            },
        )
        await _settle(lambda: woke == [False])
        # And it ends, rather than outliving the subscription it reports on.
        await changeset.aclose()
        await asyncio.wait_for(watching, 2)
    await host.stop()


async def test_a_review_of_files_this_changeset_does_not_have_is_refused() -> None:
    """The same silent no-op the empty batch is refused for -- "if none match,
    the action is a no-op" -- except this one is *knowable*: the parameter is
    `ChangesetFile.id` and the file list is in hand."""
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        changeset = await session.open_changeset(session.changesets()[0])
        with pytest.raises(InvalidArgument, match="has no files"):
            changeset.mark_reviewed("file:///work/ghost.py")
        assert _dispatched(host, "changeset/filesReviewChanged") == []
    await host.stop()


def test_the_argument_guards_are_catchable_as_client_errors() -> None:
    """ "Everything derives from `AhpClientError`, so one `except` catches the
    whole library" -- and the documented way to handle the review gate is exactly
    that `except`, which caught the capability refusal and missed the
    empty-batch one line away."""
    with pytest.raises(AhpClientError):
        actions.files_review_changed([], True)
    with pytest.raises(AhpClientError):
        actions.files_review_changed([""], True)
    # Still a ValueError, because that is what a bad argument is in Python.
    with pytest.raises(ValueError, match="no-op"):
        actions.files_review_changed([], True)


async def test_a_follow_up_content_ref_is_readable_through_the_api() -> None:
    """`InvokeChangesetOperationResult.followUp.content` is the other ContentRef
    on this surface and the one output artefact of its own write path. Without a
    public entry point a caller drops to `client.protocol.resource_read` *and*
    re-implements the base64 branch -- the hand-assembly this module exists to
    prevent."""
    host = _host(content={"ahp-changeset-content:/note": {"data": "aGk=", "encoding": "base64"}})
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        changeset = await session.open_changeset(session.changesets()[0])
        assert await changeset.read_content({"uri": "ahp-changeset-content:/note"}) == b"hi"
    await host.stop()


async def test_read_text_asks_for_the_encoding_it_wants() -> None:
    """ "The server SHOULD honor the `encoding` requested in the params." This
    call knows it wants text; saying so is the difference between reading a diff
    and base64-decoding one. A local codec name is not put on the wire, where it
    would be an undeclared enum value."""
    host = _host(
        content={"ahp-changeset-content:/bbb": {"data": "after", "encoding": "utf-8"}},
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        changeset = await session.open_changeset(session.changesets()[0])
        after = changeset.files()[0].after
        assert await changeset.read_text(after) == "after"
        assert _sent(host, "resourceRead")[-1]["encoding"] == "utf-8"
        assert _sent(host, "resourceRead")[-1]["channel"] == "ahp-root://"
    await host.stop()


def test_a_number_typed_size_hint_is_read_as_one() -> None:
    """`sizeHint`, `added` and `removed` are all `"type": "number"`; two guards
    for one type in one file made a legal `41.5` read as absent."""
    side = FileSide({"uri": "file:///a", "content": {"uri": "c", "sizeHint": 41.5}})
    assert side.size_hint == 41


def test_a_text_range_accepts_the_numbers_the_schema_declares() -> None:
    """`TextPosition.line`/`character` are `"type": "number"`, so an integral
    float is conformant -- and refusing one is a false refusal against a host
    that would take it. A fractional offset is not a position."""
    assert _text_range(
        {"start": {"line": 0, "character": 0}, "end": {"line": 1.0, "character": 4.0}}
    ) == {
        "start": {"line": 0, "character": 0},
        "end": {"line": 1, "character": 4},
    }
    with pytest.raises(InvalidArgument, match="zero-based integer"):
        _text_range({"start": {"line": 0, "character": 0}, "end": {"line": 1.5, "character": 4}})
