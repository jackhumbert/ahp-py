"""What a session says about itself, and what it can no longer say.

Five defects found by driving the sibling Python client against this host, all
of them about the gap between a channel's own state and the *projection* of it
somebody else caches:

* a catalogue entry that could be set and never un-set;
* a rename that emptied a required field;
* a seeded title two characters over its own cap;
* a dispose that stopped a turn without telling anyone it had stopped.

The fifth is here as a documented LIMITATION rather than a fix, because the
wire cannot express it -- see `TestTheSummaryCacheCannotBeToldAFieldIsGone`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.host import _TITLE_LIMIT, _title_from
from agent_host_server.provider import EchoProvider
from tests.conformance.schemas import assert_valid_action, assert_valid_state
from tests.integration.test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_FORKABLE = {"multipleChats": {"fork": True, "sideChat": True}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = Host(EchoProvider(capabilities=_FORKABLE), LoopbackSingleUserPolicy())
    try:
        yield made
    finally:
        await made.aclose()


@pytest.fixture
async def tooled() -> AsyncIterator[Host]:
    """A host whose turns open a tool call and then WAIT to be confirmed.

    The window while the tool runs is the whole subject: it is when `activity`
    is set, and when a dispose can land on a live turn.
    """
    made = Host(
        EchoProvider(capabilities=_FORKABLE, confirm_tools=True), LoopbackSingleUserPolicy()
    )
    try:
        yield made
    finally:
        await made.aclose()


async def _client(host: Host, client_id: str = "c1") -> FakeClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


async def _session(client: FakeClient, uri: str) -> str:
    """Create a session, subscribe to it, and answer with its default chat."""
    result = await client.request("createSession", {"channel": uri, "provider": "echo"})
    assert "error" not in result, result
    await client.collect(seconds=0.3)
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat: str = state["chats"][0]["resource"]
    await client.request("subscribe", {"channel": chat})
    return chat


async def _turn(client: FakeClient, chat: str, *, text: str = "hello", turn: str = "t1") -> None:
    await client.notify(
        "dispatchAction",
        {
            "channel": chat,
            "clientSeq": 1,
            "action": {
                "type": "chat/turnStarted",
                "turnId": turn,
                "startedAt": "1970-01-01T00:00:01.000Z",
                "message": {"text": text, "origin": {"kind": "user"}},
            },
        },
    )


def _state(host: Host, uri: str) -> dict[str, Any]:
    state = host.sequencer.state_of(uri)
    assert isinstance(state, dict), f"{uri} has no state"
    return state


def _entry(host: Host, session: str, chat: str) -> dict[str, Any]:
    """One chat's entry in `SessionState.chats[]`."""
    for candidate in _state(host, session)["chats"]:
        if candidate["resource"] == chat:
            return dict(candidate)
    raise AssertionError(f"{chat} is not in the catalogue")


def _terminal(client: FakeClient, chat: str) -> list[str]:
    """Every action on *chat* that ends a turn stream."""
    return [
        envelope["action"]["type"]
        for envelope in client.actions(chat)
        if envelope["action"].get("type")
        in ("chat/turnComplete", "chat/turnCancelled", "chat/error")
    ]


def _ready(client: FakeClient, chat: str) -> str:
    for envelope in client.actions(chat):
        if envelope["action"].get("type") == "chat/toolCallReady":
            call_id: str = envelope["action"]["toolCallId"]
            return call_id
    raise AssertionError("no tool call was ever announced")


class TestTheChatCatalogueCanRetractAField:
    """`session/chatUpdated` merges, so it can only ever ADD to an entry.

    `ChatSummary.activity` is the reachable case: `chat/activityChanged` sets
    it, and clearing it is an omitted key -- which the mirror turned into "no
    change", so the entry kept advertising an activity the chat channel had
    already dropped. Two host states disagreeing about one chat.
    """

    async def _tick(self, client: FakeClient, session: str, seq: int) -> None:
        """Anything at all through `dispatchAction`, to run the mirror.

        Every client dispatch on a session-owned channel ends in
        `_mirror_summary`; this is the cheapest action that reaches it.
        """
        await client.notify(
            "dispatchAction",
            {
                "channel": session,
                "clientSeq": seq,
                "action": {"type": "session/isReadChanged", "isRead": seq % 2 == 1},
            },
        )
        await client.collect(seconds=0.3)

    async def test_a_cleared_activity_leaves_the_entry(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/retract-1"
        chat = await _session(client, uri)

        # Host-originated: `chat/activityChanged` is not client-dispatchable.
        await host.sequencer.publish(
            chat, {"type": "chat/activityChanged", "activity": "Compiling"}
        )
        await self._tick(client, uri, 1)
        assert _entry(host, uri, chat)["activity"] == "Compiling"

        # Cleared the way the schema says to clear it: "omit or set `undefined`".
        await host.sequencer.publish(chat, {"type": "chat/activityChanged"})
        await self._tick(client, uri, 2)

        assert "activity" not in _entry(host, uri, chat), (
            "the catalogue kept an activity the chat channel had already cleared"
        )
        assert _state(host, uri)["chats"][0]["resource"] == chat
        assert_valid_state("session", _state(host, uri))

    async def test_the_retraction_is_an_upsert_a_client_can_apply(self, host: Host) -> None:
        """Published as `session/chatAdded`, which is the only retraction the
        catalogue has: "if a chat with the same `summary.resource` already
        exists, the existing entry is replaced"."""
        client = await _client(host)
        uri = "echo:/retract-2"
        chat = await _session(client, uri)
        await host.sequencer.publish(chat, {"type": "chat/activityChanged", "activity": "Indexing"})
        await self._tick(client, uri, 1)
        client.notifications.clear()

        await host.sequencer.publish(chat, {"type": "chat/activityChanged"})
        await self._tick(client, uri, 2)

        upserts = [
            envelope["action"]
            for envelope in client.actions(uri)
            if envelope["action"].get("type") == "session/chatAdded"
        ]
        assert len(upserts) == 1, f"expected one upsert, saw {len(upserts)}"
        assert_valid_action(upserts[0])
        assert "activity" not in upserts[0]["summary"]
        # Wholesale replacement, so identity has to survive it.
        assert upserts[0]["summary"]["resource"] == chat
        assert upserts[0]["summary"]["title"]

    async def test_no_wire_frame_ever_carries_a_null(self, host: Host) -> None:
        """The obvious fix -- `changes: {activity: null}` -- is a schema
        violation: every field of `changes` is typed as its own non-null type.
        """
        client = await _client(host)
        uri = "echo:/retract-3"
        chat = await _session(client, uri)
        await host.sequencer.publish(chat, {"type": "chat/activityChanged", "activity": "Linting"})
        await self._tick(client, uri, 1)
        await host.sequencer.publish(chat, {"type": "chat/activityChanged"})
        await self._tick(client, uri, 2)

        for envelope in client.actions(uri):
            assert_valid_action(envelope["action"])
        for note in client.notifications:
            if note.get("method") == "root/sessionSummaryChanged":
                assert None not in note["params"]["changes"].values()

    async def test_an_unknown_chat_is_not_added_by_a_retraction(self, host: Host) -> None:
        """`session/chatAdded` ADDS when the resource is new, and a retraction
        must never do that -- it would resurrect a chat the catalogue had
        removed."""
        client = await _client(host)
        uri = "echo:/retract-4"
        chat = await _session(client, uri)
        await host.sequencer.publish(chat, {"type": "chat/activityChanged", "activity": "Working"})
        await self._tick(client, uri, 1)
        # Drop the entry out from under the mirror, leaving `published_chats`
        # still holding the field that is about to be retracted.
        session = host._sessions[uri]
        await host.sequencer.publish(uri, {"type": "session/chatRemoved", "chat": chat})
        assert not _state(host, uri)["chats"]
        client.notifications.clear()

        await host.sequencer.publish(chat, {"type": "chat/activityChanged"})
        await host._mirror_summary(session)
        await client.collect(seconds=0.3)

        assert not _state(host, uri)["chats"], "a retraction re-added a removed chat"


class TestTheSummaryCacheCannotBeToldAFieldIsGone:
    """`root/sessionSummaryChanged` has no encoding for a retraction.

    This is a LIMITATION, pinned rather than fixed, and the assertions here say
    exactly where the line is: the host's authoritative answers are correct,
    every frame it emits is schema-valid, and the incremental cache a client
    builds from `root/sessionAdded` + `root/sessionSummaryChanged` is the one
    thing that goes stale. Fixing it means either `null` in `changes` (which
    `notifications.schema.json` types as a string) or re-announcing the session
    (which no notification documents as an upsert) -- inventing wire semantics
    unilaterally, which this host does not do. `docs/research.md` §11.
    """

    async def _cache(self, client: FakeClient, uri: str) -> dict[str, Any]:
        cached: dict[str, Any] = {}
        for note in client.notifications:
            params = note.get("params") or {}
            if note.get("method") == "root/sessionAdded" and params["summary"]["resource"] == uri:
                cached = dict(params["summary"])
            if note.get("method") == "root/sessionSummaryChanged" and params["session"] == uri:
                cached.update(params["changes"])
        return cached

    async def _run_a_tool_turn(self, host: Host, client: FakeClient, uri: str, chat: str) -> None:
        await _turn(client, chat)
        await client.collect(seconds=0.5)
        assert _state(host, uri).get("activity") == "Echo Tool"
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 3,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": _ready(client, chat),
                    "approved": True,
                    # `ChatToolCallApprovedAction` requires it: "user-action"
                    # is the human clicking Allow, which is what this is.
                    "confirmed": "user-action",
                },
            },
        )
        await client.collect(seconds=0.6)

    async def test_the_authoritative_catalogue_is_right(self, tooled: Host) -> None:
        client = await _client(tooled)
        uri = "echo:/cache-1"
        chat = await _session(client, uri)
        await self._run_a_tool_turn(tooled, client, uri, chat)

        assert "activity" not in _state(tooled, uri)
        listed = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]["items"]
        entry = next(item for item in listed if item["resource"] == uri)
        assert "activity" not in entry, "listSessions is the truth and must not be stale"

    async def test_the_incremental_cache_keeps_the_last_activity(self, tooled: Host) -> None:
        """The defect, stated as a fact about the protocol rather than about
        this host. If upstream grows a retraction token, this test is the one
        that flips."""
        client = await _client(tooled)
        uri = "echo:/cache-2"
        chat = await _session(client, uri)
        await self._run_a_tool_turn(tooled, client, uri, chat)

        cached = await self._cache(client, uri)
        assert cached.get("activity") == "Echo Tool", (
            "if this now agrees with listSessions, the wire learned how to "
            "retract a field -- delete the limitation from `_mirror_summary`"
        )
        # Every OTHER field converges: the hole is retraction, not the mirror.
        listed = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]["items"]
        truth = next(item for item in listed if item["resource"] == uri)
        assert {k: v for k, v in cached.items() if k != "activity"} == {
            k: v for k, v in truth.items() if k != "activity"
        }


class TestARenameNeedsATitle:
    """`SessionTitleChangedAction` declares `"required": ["type", "title"]`."""

    async def _rename(self, client: FakeClient, uri: str, seq: int, **payload: Any) -> None:
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": seq,
                "action": {"type": "session/titleChanged", **payload},
            },
        )
        await client.collect(seconds=0.3)

    def _rejections(self, client: FakeClient, uri: str) -> list[str]:
        return [
            envelope["rejectionReason"]
            for envelope in client.actions(uri)
            if envelope.get("rejectionReason")
        ]

    @pytest.mark.parametrize("payload", [{}, {"title": None}, {"title": 7}, {"title": ["a"]}])
    async def test_a_titleless_rename_is_refused(self, host: Host, payload: Any) -> None:
        client = await _client(host)
        uri = "echo:/title-1"
        await _session(client, uri)
        before = _state(host, uri)["title"]

        await self._rename(client, uri, 1, **payload)

        assert _state(host, uri)["title"] == before
        assert_valid_state("session", _state(host, uri))
        assert self._rejections(client, uri), "dropped silently instead of echoed"

    async def test_a_real_rename_still_works(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/title-2"
        await _session(client, uri)

        await self._rename(client, uri, 1, title="Refactor the parser")

        assert _state(host, uri)["title"] == "Refactor the parser"
        assert not self._rejections(client, uri)

    async def test_the_cached_summary_follows_a_real_rename(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/title-3"
        await _session(client, uri)
        await self._rename(client, uri, 1, title="Ship it")

        renames = [
            note["params"]["changes"]["title"]
            for note in client.notifications
            if note.get("method") == "root/sessionSummaryChanged"
            and note["params"]["session"] == uri
            and "title" in note["params"]["changes"]
        ]
        assert renames[-1] == "Ship it"


class TestTheSeededTitleFitsItsCap:
    """The ellipsis is a character of the title, and it was not budgeted for."""

    def test_one_enormous_word_fits(self) -> None:
        title = _title_from("https://example.com/" + "a" * 200)
        assert title is not None
        assert title.endswith("…")
        assert len(title) == _TITLE_LIMIT

    def test_a_word_boundary_fits(self) -> None:
        title = _title_from("please " * 40)
        assert title is not None
        assert len(title) <= _TITLE_LIMIT
        assert title.rstrip("…").split()[-1] == "please"

    @pytest.mark.parametrize(
        "text",
        [
            "x" * 61,
            "x" * 60,
            "x" * 59 + " " + "y" * 40,
            "ab " * 40,
            "x" * 40 + " " + "y" * 40,
            "word " + "z" * 300,
        ],
    )
    def test_never_over_the_cap(self, text: str) -> None:
        title = _title_from(text)
        assert title is not None
        assert len(title) <= _TITLE_LIMIT, f"{len(title)} > {_TITLE_LIMIT}: {title!r}"

    def test_exactly_at_the_cap_is_not_truncated(self) -> None:
        """A title that already fits keeps its last character and its ellipsis
        is not spent."""
        assert _title_from("x" * _TITLE_LIMIT) == "x" * _TITLE_LIMIT


class TestDisposeEndsTheTurnItStops:
    """A turn stream ends on one of three actions, or it does not end."""

    async def _park_a_turn(self, host: Host, client: FakeClient, chat: str) -> None:
        """Start a turn and leave it waiting on a tool confirmation."""
        await _turn(client, chat, text="run it", turn="live")
        await client.collect(seconds=0.5)
        assert _state(host, chat)["activeTurn"]["id"] == "live"

    async def test_dispose_session_cancels_the_turn_on_the_wire(self, tooled: Host) -> None:
        client = await _client(tooled)
        uri = "echo:/dispose-1"
        chat = await _session(client, uri)
        await self._park_a_turn(tooled, client, chat)
        client.notifications.clear()

        await client.request("disposeSession", {"channel": uri})
        await client.collect(seconds=0.4)

        assert _terminal(client, chat) == ["chat/turnCancelled"]

    async def test_the_cancellation_names_the_turn_and_is_schema_valid(self, tooled: Host) -> None:
        client = await _client(tooled)
        uri = "echo:/dispose-2"
        chat = await _session(client, uri)
        await self._park_a_turn(tooled, client, chat)
        client.notifications.clear()

        await client.request("disposeSession", {"channel": uri})
        await client.collect(seconds=0.4)

        cancelled = next(
            envelope["action"]
            for envelope in client.actions(chat)
            if envelope["action"]["type"] == "chat/turnCancelled"
        )
        assert cancelled["turnId"] == "live"
        # Measured, not zero: a client renders it as the turn's elapsed time.
        assert cancelled["duration"] > 0
        assert_valid_action(cancelled)

    async def test_an_idle_session_is_disposed_silently(self, tooled: Host) -> None:
        """Nothing to end, so nothing is said. A terminal action for a turn
        that never ran would be a lie in every transcript."""
        client = await _client(tooled)
        uri = "echo:/dispose-3"
        chat = await _session(client, uri)
        client.notifications.clear()

        await client.request("disposeSession", {"channel": uri})
        await client.collect(seconds=0.4)

        assert _terminal(client, chat) == []

    async def test_dispose_chat_cancels_only_that_chats_turn(self, tooled: Host) -> None:
        client = await _client(tooled)
        uri = "echo:/dispose-4"
        default = await _session(client, uri)

        # A side chat needs a landed source turn, so the default chat runs one
        # first -- and finishes it, or `createChat` refuses the source.
        await _turn(client, default, text="seed", turn="seed")
        await client.collect(seconds=0.5)
        await client.notify(
            "dispatchAction",
            {
                "channel": default,
                "clientSeq": 3,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "seed",
                    "toolCallId": _ready(client, default),
                    "approved": True,
                    # `ChatToolCallApprovedAction` requires it: "user-action"
                    # is the human clicking Allow, which is what this is.
                    "confirmed": "user-action",
                },
            },
        )
        await client.collect(seconds=0.6)
        created = await client.request(
            "createChat",
            {
                "channel": uri,
                "chat": "ahp-chat:/aside",
                "source": {"kind": "sideChat", "chat": default, "turnId": "seed"},
            },
        )
        assert "error" not in created, created
        await client.request("subscribe", {"channel": "ahp-chat:/aside"})

        await self._park_a_turn(tooled, client, "ahp-chat:/aside")
        client.notifications.clear()
        await client.request("disposeChat", {"channel": "ahp-chat:/aside"})
        await client.collect(seconds=0.4)

        assert _terminal(client, "ahp-chat:/aside") == ["chat/turnCancelled"]
        assert _terminal(client, default) == [], "the innocent chat was touched"
