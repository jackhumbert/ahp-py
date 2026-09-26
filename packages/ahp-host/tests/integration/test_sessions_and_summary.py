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
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.host import _TITLE_LIMIT, _title_from
from ahp_host.provider import EchoProvider
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
    # The announcement, not a moment: several tests here rebuild a client-side
    # cache out of `root/sessionAdded` + `root/sessionSummaryChanged`, so the
    # first of those frames has to be in `notifications` before they start.
    await client.collect_until(lambda: _announced(client, uri), timeout=10.0)
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


def _catalogued(host: Host, session: str, chat: str) -> dict[str, Any]:
    """One chat's entry in `SessionState.chats[]`, or `{}` when it has none.

    The non-raising twin of `_entry`: a wait predicate has to be able to answer
    "not yet" without blowing up, since it is called before the thing it is
    waiting for has happened.
    """
    for candidate in _state(host, session)["chats"]:
        if candidate["resource"] == chat:
            return dict(candidate)
    return {}


def _entry(host: Host, session: str, chat: str) -> dict[str, Any]:
    """One chat's entry in `SessionState.chats[]`."""
    found = _catalogued(host, session, chat)
    if not found:
        raise AssertionError(f"{chat} is not in the catalogue")
    return found


def _announced(client: FakeClient, uri: str) -> bool:
    """Has the root channel announced *uri* yet?"""
    return any(
        note.get("method") == "root/sessionAdded"
        and (note.get("params") or {})["summary"]["resource"] == uri
        for note in client.notifications
    )


def _active_turn(host: Host, chat: str) -> str | None:
    """The id of *chat*'s active turn, or `None` -- safe inside a predicate."""
    active = (host.sequencer.state_of(chat) or {}).get("activeTurn")
    return str(active["id"]) if isinstance(active, dict) else None


def _landed(host: Host, chat: str, turn: str) -> bool:
    """Has *turn* reached *chat*'s completed list?

    `createChat` refuses a source turn that has not landed, so a wait for this
    is a wait for the next call to be legal rather than for a plausible moment.
    """
    return any(t.get("id") == turn for t in (host.sequencer.state_of(chat) or {}).get("turns", []))


def _terminal(client: FakeClient, chat: str) -> list[str]:
    """Every action on *chat* that ends a turn stream."""
    return [
        envelope["action"]["type"]
        for envelope in client.actions(chat)
        if envelope["action"].get("type")
        in ("chat/turnComplete", "chat/turnCancelled", "chat/error")
    ]


def _announced_call(client: FakeClient, chat: str) -> str | None:
    """The id of the tool call *chat* is waiting on, or `None`.

    The non-raising twin of `_ready`, for use inside a wait predicate.
    """
    for envelope in client.actions(chat):
        if envelope["action"].get("type") == "chat/toolCallReady":
            call_id: str = envelope["action"]["toolCallId"]
            return call_id
    return None


def _ready(client: FakeClient, chat: str) -> str:
    call_id = _announced_call(client, chat)
    if call_id is None:
        raise AssertionError("no tool call was ever announced")
    return call_id


class TestTheChatCatalogueCanRetractAField:
    """`session/chatUpdated` merges, so it can only ever ADD to an entry.

    `ChatSummary.activity` is the reachable case: `chat/activityChanged` sets
    it, and clearing it is an omitted key -- which the mirror turned into "no
    change", so the entry kept advertising an activity the chat channel had
    already dropped. Two host states disagreeing about one chat.
    """

    async def _tick(
        self,
        client: FakeClient,
        session: str,
        seq: int,
        until: Callable[[], bool] | None = None,
    ) -> None:
        """Anything at all through `dispatchAction`, to run the mirror.

        Every client dispatch on a session-owned channel ends in
        `_mirror_summary`; this is the cheapest action that reaches it.

        *until* is what the caller expects the mirror to have DONE, waited for
        directly. `None` keeps a real elapsed wait, which is what a caller
        asserting over *everything* the mirror emitted -- or over what it must
        NOT emit -- needs; a condition would return on the first frame and leave
        the rest of them unexamined in the transport.
        """
        await client.notify(
            "dispatchAction",
            {
                "channel": session,
                "clientSeq": seq,
                "action": {"type": "session/isReadChanged", "isRead": seq % 2 == 1},
            },
        )
        if until is None:
            await client.collect(seconds=0.3)
        else:
            await client.collect_until(until, timeout=10.0)

    async def test_a_cleared_activity_leaves_the_entry(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/retract-1"
        chat = await _session(client, uri)

        # Host-originated: `chat/activityChanged` is not client-dispatchable.
        await host.sequencer.publish(
            chat, {"type": "chat/activityChanged", "activity": "Compiling"}
        )
        await self._tick(
            client, uri, 1, lambda: _catalogued(host, uri, chat).get("activity") == "Compiling"
        )
        assert _entry(host, uri, chat)["activity"] == "Compiling"

        # Cleared the way the schema says to clear it: "omit or set `undefined`".
        await host.sequencer.publish(chat, {"type": "chat/activityChanged"})
        # The retraction landing IS the assertion below, so it is what we wait
        # for: it starts false (the entry still says "Compiling") and the wait
        # ends when the mirror clears it, or times out and lets the assertion
        # say which activity was left behind.
        await self._tick(client, uri, 2, lambda: "activity" not in _catalogued(host, uri, chat))

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
        await self._tick(
            client, uri, 1, lambda: _catalogued(host, uri, chat).get("activity") == "Indexing"
        )
        client.notifications.clear()

        await host.sequencer.publish(chat, {"type": "chat/activityChanged"})
        # Elapsed, deliberately: "expected ONE upsert" is an assertion about the
        # second one not existing, and a condition wait would return on the
        # first and leave a duplicate sitting undelivered in the transport.
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
        await self._tick(
            client, uri, 1, lambda: _catalogued(host, uri, chat).get("activity") == "Linting"
        )
        await host.sequencer.publish(chat, {"type": "chat/activityChanged"})
        # Elapsed, deliberately: the assertions below run over EVERY frame that
        # arrived, so stopping at the first interesting one would quietly shrink
        # the set of frames being checked for a null.
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
        await self._tick(
            client, uri, 1, lambda: _catalogued(host, uri, chat).get("activity") == "Working"
        )
        # Drop the entry out from under the mirror, leaving `published_chats`
        # still holding the field that is about to be retracted.
        session = host._sessions[uri]
        await host.sequencer.publish(uri, {"type": "session/chatRemoved", "chat": chat})
        assert not _state(host, uri)["chats"]
        client.notifications.clear()

        await host.sequencer.publish(chat, {"type": "chat/activityChanged"})
        await host._mirror_summary(session)
        # Elapsed, deliberately: the assertion is that nothing re-added the
        # chat, and a wait for "still empty" would be satisfied instantly and
        # prove nothing.
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
        # BOTH halves of "the tool is running and waiting": the session's
        # activity is published before the chat's `chat/toolCallReady` reaches
        # this client, so waiting on the activity alone leaves the confirmation
        # below with no call id to name.
        await client.collect_until(
            lambda: (
                _state(host, uri).get("activity") == "Echo Tool"
                and _announced_call(client, chat) is not None
            ),
            timeout=10.0,
        )
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
        # Elapsed, deliberately. `test_the_incremental_cache_keeps_the_last_activity`
        # rebuilds the client's cache from every `root/sessionSummaryChanged`
        # and then compares it field-for-field with `listSessions`, so it needs
        # the WHOLE tail of the turn delivered -- and `_mirror_summary` emits
        # that notification last, after the state change any condition here
        # would key on. Stopping at the state change would leave the final
        # frame in the transport and make the comparison a race.
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

    async def _rename(
        self,
        client: FakeClient,
        uri: str,
        seq: int,
        until: Callable[[], bool],
        **payload: Any,
    ) -> None:
        """Dispatch a rename and wait for the host's answer to it.

        *until* is that answer -- the new title, or the rejection. Waiting for
        one of them is what makes the accompanying negative assertion ("and the
        title did not change", "and nothing was rejected") mean anything: the
        dispatch has demonstrably been processed by then.
        """
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": seq,
                "action": {"type": "session/titleChanged", **payload},
            },
        )
        await client.collect_until(until, timeout=10.0)

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

        await self._rename(client, uri, 1, lambda: bool(self._rejections(client, uri)), **payload)

        assert _state(host, uri)["title"] == before
        assert_valid_state("session", _state(host, uri))
        assert self._rejections(client, uri), "dropped silently instead of echoed"

    async def test_a_real_rename_still_works(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/title-2"
        await _session(client, uri)

        await self._rename(
            client,
            uri,
            1,
            lambda: _state(host, uri)["title"] == "Refactor the parser",
            title="Refactor the parser",
        )

        assert _state(host, uri)["title"] == "Refactor the parser"
        assert not self._rejections(client, uri)

    def _cached_titles(self, client: FakeClient, uri: str) -> list[str]:
        return [
            note["params"]["changes"]["title"]
            for note in client.notifications
            if note.get("method") == "root/sessionSummaryChanged"
            and note["params"]["session"] == uri
            and "title" in note["params"]["changes"]
        ]

    async def test_the_cached_summary_follows_a_real_rename(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/title-3"
        # The end state is the cache's LAST word on the title, which is what the
        # assertion reads -- not merely that some frame mentioned it.
        await _session(client, uri)
        await self._rename(
            client,
            uri,
            1,
            lambda: self._cached_titles(client, uri)[-1:] == ["Ship it"],
            title="Ship it",
        )

        assert self._cached_titles(client, uri)[-1] == "Ship it"


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
        # Parked, not merely started: the tool call has to have been ANNOUNCED
        # for the turn to be waiting on a confirmation, which is the state every
        # caller of this then disposes out from under.
        await client.collect_until(
            lambda: (
                _active_turn(host, chat) == "live" and _announced_call(client, chat) is not None
            ),
            timeout=10.0,
        )
        assert _state(host, chat)["activeTurn"]["id"] == "live"

    async def test_dispose_session_cancels_the_turn_on_the_wire(self, tooled: Host) -> None:
        client = await _client(tooled)
        uri = "echo:/dispose-1"
        chat = await _session(client, uri)
        await self._park_a_turn(tooled, client, chat)
        client.notifications.clear()

        await client.request("disposeSession", {"channel": uri})
        # Elapsed, deliberately: "exactly one terminal action" is an assertion
        # that a SECOND one never arrives, and a condition wait would return on
        # the first and never see the duplicate it is there to catch.
        await client.collect(seconds=0.4)

        assert _terminal(client, chat) == ["chat/turnCancelled"]

    async def test_the_cancellation_names_the_turn_and_is_schema_valid(self, tooled: Host) -> None:
        client = await _client(tooled)
        uri = "echo:/dispose-2"
        chat = await _session(client, uri)
        await self._park_a_turn(tooled, client, chat)
        client.notifications.clear()
        # Load-bearing, and small on purpose. `duration` is whole milliseconds
        # of measured elapsed time, so the turn has to actually run for a
        # measurable moment before it is disposed or the `> 0` below stops
        # being a fact about the host and starts being one about the machine.
        await asyncio.sleep(0.01)

        await client.request("disposeSession", {"channel": uri})
        await client.collect_until(
            lambda: bool(_terminal(client, chat)),
            timeout=10.0,
        )

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
        # Elapsed, deliberately: the assertion is that nothing was said at all,
        # and a wait for silence is over before it starts.
        await client.collect(seconds=0.4)

        assert _terminal(client, chat) == []

    async def test_dispose_chat_cancels_only_that_chats_turn(self, tooled: Host) -> None:
        client = await _client(tooled)
        uri = "echo:/dispose-4"
        default = await _session(client, uri)

        # A side chat needs a landed source turn, so the default chat runs one
        # first -- and finishes it, or `createChat` refuses the source.
        await _turn(client, default, text="seed", turn="seed")
        await client.collect_until(
            lambda: _announced_call(client, default) is not None, timeout=10.0
        )
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
        # Landed, not "probably landed": `createChat` refuses a source turn that
        # is not in the chat's completed list, and the failure mode of getting
        # this wrong is an error response the next line asserts past.
        await client.collect_until(lambda: _landed(tooled, default, "seed"), timeout=10.0)
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
        # Elapsed, deliberately: the assertion that matters here is the negative
        # one -- the sibling chat was NOT touched -- and it needs real time in
        # which the host could have touched it.
        await client.collect(seconds=0.4)

        assert _terminal(client, "ahp-chat:/aside") == ["chat/turnCancelled"]
        assert _terminal(client, default) == [], "the innocent chat was touched"
