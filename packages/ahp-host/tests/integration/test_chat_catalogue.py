"""The chat catalogue, and the one gap where the user is told something untrue.

`SessionState.chats[]` is what a client renders its chat tabs from, and it was
written once at `session/chatAdded` and never touched again -- so every tab
stayed idle, unnamed, and stamped with the moment it was created, however much
work happened inside it. `ChatState` "inlines (denormalizes) every field" the
catalogue carries, which means the two can disagree and only the host can stop
them.

Truncation is worse than stale. The reducer drops the turns, so edit-and-resend
looks right; the agent goes on remembering them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.reducers.clock import now_iso
from ahp_protocol.transport import memory_pair
from ahp_protocol.types.protocol import SessionStatus

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import AgentSessionContext, TruncatesHistory
from ahp_host.provider.echo import EchoSession

from .test_host_end_to_end import FakeClient

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
async def slow() -> AsyncIterator[Host]:
    made = Host(EchoProvider(capabilities=_FORKABLE, delay=0.3), LoopbackSingleUserPolicy())
    try:
        yield made
    finally:
        await made.aclose()


async def _client(host: Host) -> FakeClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "c1",
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


async def _session(host: Host, client: FakeClient, uri: str) -> str:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("chats")), timeout=10.0
    )
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
                # Real time, not a fixed stamp: since 0.9.0 the chat's
                # `modifiedAt` is derived from this (plus the turn's duration).
                "startedAt": now_iso(),
                "message": {"text": text, "origin": {"kind": "user"}},
            },
        },
    )


def _state(host: Host, uri: str) -> dict[str, Any]:
    state = host.sequencer.state_of(uri)
    assert isinstance(state, dict)
    return state


def _catalogue(host: Host, session: str, chat: str) -> dict[str, Any]:
    entry = next(c for c in _state(host, session)["chats"] if c["resource"] == chat)
    assert isinstance(entry, dict)
    return entry


def _actions(client: FakeClient, channel: str) -> list[dict[str, Any]]:
    return [
        note["params"]["action"]
        for note in client.notifications
        if note.get("method") == "action" and note["params"].get("channel") == channel
    ]


def _truncations(host: Host, uri: str) -> list[Any]:
    """What the provider has been told to forget, if it can be told at all."""
    return list(getattr(host._sessions[uri].agent_session, "truncated", []))


def _entry(host: Host, session: str, chat: str) -> dict[str, Any]:
    """The catalogue entry for *chat*, or `{}` while there is not one yet.

    Total where :func:`_catalogue` is not, because this is read from inside a
    wait predicate: a `StopIteration` out of a condition is a crash, not a
    "not yet".
    """
    entries = (host.sequencer.state_of(session) or {}).get("chats") or []
    empty: dict[str, Any] = {}
    found = next((e for e in entries if e.get("resource") == chat), empty)
    assert isinstance(found, dict)
    return found


async def _ran(
    host: Host, client: FakeClient, chat: str, turn: str, *, timeout: float = 10.0
) -> None:
    """Wait for *turn* to FINISH and land in *chat*'s history.

    Its terminal state, not its existence: a turn still in flight is held in
    `activeTurn`, and history that is one publish short is the same race a
    fixed wait is, only quieter.
    """
    await client.collect_until(
        lambda: (
            (host.sequencer.state_of(chat) or {}).get("activeTurn") is None
            and any(
                t.get("id") == turn
                for t in (host.sequencer.state_of(chat) or {}).get("turns") or []
            )
        ),
        timeout=timeout,
    )


def _restated(client: FakeClient, session: str, chat: str, entry: dict[str, Any]) -> bool:
    """Has the client seen a frame that leaves *chat*'s entry as *entry* is?

    Matched by CONTENT rather than by count or by any single field: the mirror
    publishes only what changed, so which fields a frame carries depends on
    what moved -- `modifiedAt` is millisecond-stamped and a turn fast enough to
    start and finish inside one millisecond does not move it at all. A frame
    every key of which already agrees with the settled entry is the frame that
    settled it, whichever fields that turned out to be.

    A retraction goes out as `session/chatAdded`, the catalogue's only way to
    un-set a field, so that counts as a restatement too.
    """
    for action in _actions(client, session):
        summary = action.get("summary") or {}
        if action["type"] == "session/chatUpdated" and action.get("chat") == chat:
            changes: dict[str, Any] = action.get("changes") or {}
        elif action["type"] == "session/chatAdded" and summary.get("resource") == chat:
            changes = {k: v for k, v in summary.items() if k != "resource"}
        else:
            continue
        if changes and all(entry.get(key) == value for key, value in changes.items()):
            return True
    return False


async def _mirrored(
    host: Host, client: FakeClient, session: str, chat: str, *, timeout: float = 10.0
) -> None:
    """Wait until the client has SEEN the catalogue catch up with *chat*.

    Three conditions, because the catalogue trails the thing these tests are
    tempted to wait for. The turn has to be over; the mirror runs AFTER
    `chat/turnComplete` is published, so "the turn finished" returns one
    publish early and leaves the entry stale; and the host's own state moves
    before the frame reaches the wire, so a test that reads
    `client.notifications` needs the frame and not the state.
    """

    def ready() -> bool:
        state = host.sequencer.state_of(chat) or {}
        if state.get("activeTurn") is not None or not state.get("turns"):
            return False
        entry = _entry(host, session, chat)
        if entry.get("modifiedAt") != state.get("modifiedAt"):
            return False
        return _restated(client, session, chat, entry)

    await client.collect_until(ready, timeout=timeout)


class TestTheDefaultChatIsNamedAsAChat:
    async def test_it_is_not_given_the_sessions_name(self, host: Host) -> None:
        """A chat tab reading "New Session" is a tab labelled with the name of
        the thing that contains it. `ChatSummary.title` is REQUIRED, so it
        cannot simply be omitted."""
        client = await _client(host)
        uri = "echo:/c-1"
        chat = await _session(host, client, uri)

        assert _catalogue(host, uri, chat)["title"] == "New Chat"

    async def test_naming_the_session_does_not_rename_the_chat(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/c-2"
        chat = await _session(host, client, uri)

        await _turn(client, chat, text="add a retry to the fetch helper")
        await _mirrored(host, client, uri, chat)

        assert _state(host, uri)["title"] == "add a retry to the fetch helper"
        assert _catalogue(host, uri, chat)["title"] == "New Chat"


class TestTheCatalogueKeepsUp:
    async def test_a_finished_turn_moves_the_entry(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/c-3"
        chat = await _session(host, client, uri)
        before = dict(_catalogue(host, uri, chat))

        await _turn(client, chat, text="hello")
        await _mirrored(host, client, uri, chat)

        after = _catalogue(host, uri, chat)
        # `>=`, not `>`. `modifiedAt` has MILLISECOND resolution, and once the
        # fixed sleeps came out of this suite the whole flow -- create, turn,
        # mirror -- completes inside one tick, so a strict `>` was asserting
        # that the clock had moved rather than that the catalogue had. It
        # failed under random ordering with both stamps reading the same
        # millisecond.
        #
        # The line below is the assertion that was always doing the work: the
        # entry tracks the CHAT's own timestamp. That is what "the catalogue
        # keeps up" means, and it holds whether or not a millisecond elapsed.
        assert after["modifiedAt"] >= before["modifiedAt"]
        assert after["modifiedAt"] == _state(host, chat)["modifiedAt"], "catalogue drifted"
        assert after["status"] == _state(host, chat)["status"], "catalogue drifted"

    async def test_a_working_chat_says_so_in_the_catalogue(self, slow: Host) -> None:
        client = await _client(slow)
        uri = "echo:/c-4"
        chat = await _session(slow, client, uri)

        await _turn(client, chat, text="hello there this takes a while")
        # Kept: a sample taken WHILE the turn runs. A condition wait here would
        # be waiting for the very thing the next line asserts.
        await asyncio.sleep(0.25)
        during = _catalogue(slow, uri, chat)["status"]

        await _mirrored(slow, client, uri, chat)
        assert during & SessionStatus.IN_PROGRESS, "the tab looked idle while it worked"
        assert not _catalogue(slow, uri, chat)["status"] & SessionStatus.IN_PROGRESS

    async def test_it_is_published_as_a_partial_update(self, host: Host) -> None:
        """ "Only fields present in `changes` are written; omitted fields are
        preserved", and `resource` "MUST NOT be carried in `changes`" -- it is
        identity, not data."""
        client = await _client(host)
        uri = "echo:/c-5"
        chat = await _session(host, client, uri)
        await client.request("subscribe", {"channel": uri})
        client.notifications.clear()

        await _turn(client, chat, text="hello")
        await _mirrored(host, client, uri, chat)

        updates = [a for a in _actions(client, uri) if a["type"] == "session/chatUpdated"]
        assert updates, "session/chatUpdated was never emitted"
        for update in updates:
            assert update["chat"] == chat
            assert "resource" not in update["changes"]
            assert update["changes"], "an empty change set is a wasted frame"

    async def test_nothing_is_published_when_nothing_moved(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/c-6"
        await _session(host, client, uri)
        await client.request("subscribe", {"channel": uri})
        # Both waits stay fixed. The assertion is that NOTHING was published:
        # the first has to drain the stream to quiet, or a frame still in
        # flight from setup survives the clear and lands in the window below,
        # and the second has to be a real elapsed moment or it proves nothing.
        await client.collect(seconds=0.4)
        client.notifications.clear()

        session = host._sessions[uri]
        for _ in range(3):
            await host._mirror_summary(session)
        await client.collect(seconds=0.3)

        assert not [a for a in _actions(client, uri) if a["type"] == "session/chatUpdated"]

    async def test_a_side_chat_gets_its_own_entry(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/c-7"
        default = await _session(host, client, uri)
        await _turn(client, default, text="seed", turn="seed-turn")
        # `createChat` refuses a source turn that has not landed, so this is a
        # precondition and not decoration.
        await _ran(host, client, default, "seed-turn")

        result = await client.request(
            "createChat",
            {
                "channel": uri,
                "chat": "ahp-chat:/aside",
                "source": {"kind": "sideChat", "chat": default, "turnId": "seed-turn"},
            },
        )
        assert "error" not in result, result
        await client.request("subscribe", {"channel": "ahp-chat:/aside"})
        await _turn(client, "ahp-chat:/aside", text="what is this?", turn="a1")
        await _mirrored(host, client, uri, "ahp-chat:/aside")

        entry = _catalogue(host, uri, "ahp-chat:/aside")
        assert entry["modifiedAt"] == _state(host, "ahp-chat:/aside")["modifiedAt"]


class TestTruncation:
    """The most dangerous of the parity gaps: the user is not underserved, they
    are misinformed."""

    async def _truncate(self, client: FakeClient, chat: str, seq: int, **extra: Any) -> None:
        """Dispatch the action, and nothing more.

        Waiting belongs to the caller: what "done" means differs per test --
        a provider told, a turn dropped, a refusal echoed, or, for the explicit
        null, an elapsed moment in which none of that may happen.
        """
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": seq,
                "action": {"type": "chat/truncated", **extra},
            },
        )

    async def test_the_provider_is_told(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/x-1"
        chat = await _session(host, client, uri)
        await _turn(client, chat, text="hello", turn="t1")
        await _ran(host, client, chat, "t1")

        await self._truncate(client, chat, 2, turnId="t1")
        await client.collect_until(lambda: bool(_truncations(host, uri)), timeout=10.0)

        session = host._sessions[uri].agent_session
        assert isinstance(session, TruncatesHistory | EchoSession)
        assert session.truncated == [(chat, "t1")]

    async def test_an_absent_turn_id_means_everything(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/x-2"
        chat = await _session(host, client, uri)
        await _turn(client, chat, text="hello")
        await _ran(host, client, chat, "t1")

        await self._truncate(client, chat, 2)
        await client.collect_until(
            lambda: bool(_truncations(host, uri)) and _state(host, chat)["turns"] == [],
            timeout=10.0,
        )

        assert host._sessions[uri].agent_session.truncated == [(chat, None)]  # type: ignore[union-attr]
        assert _state(host, chat)["turns"] == []

    async def test_an_explicit_null_is_not_the_same_as_absent(self, host: Host) -> None:
        """The reducer searches for a turn with that id, finds none and no-ops.
        Collapsing null into absent here would have the agent forget a whole
        conversation the client still shows -- this defect with the sides
        swapped."""
        client = await _client(host)
        uri = "echo:/x-3"
        chat = await _session(host, client, uri)
        await _turn(client, chat, text="hello")
        await _ran(host, client, chat, "t1")

        await self._truncate(client, chat, 2, turnId=None)
        # Fixed, and staying fixed: both assertions below are that NOTHING
        # happened, and a condition wait would return at once and prove it.
        await client.collect(seconds=0.5)

        assert host._sessions[uri].agent_session.truncated == []  # type: ignore[union-attr]
        assert _state(host, chat)["turns"], "the reducer dropped turns on an explicit null"

    async def test_a_running_turn_is_dropped(self, slow: Host) -> None:
        """ "If there is an active turn it is silently dropped and the chat
        status returns to `idle`." The reducer does the status; without the task
        being cancelled the turn keeps publishing deltas into a transcript with
        nothing to hang them on."""
        client = await _client(slow)
        uri = "echo:/x-4"
        chat = await _session(slow, client, uri)

        await _turn(client, chat, text="hello there this takes a while")
        # Waiting for the turn to be RUNNING, rather than sleeping into the
        # middle of it: the truncation has to land on a live turn for this test
        # to be testing anything, and arriving sooner leaves more of the turn.
        await client.collect_until(
            lambda: (slow.sequencer.state_of(chat) or {}).get("activeTurn") is not None,
            timeout=10.0,
        )
        assert _state(slow, chat)["activeTurn"] is not None

        await self._truncate(client, chat, 2)
        await client.collect_until(
            lambda: (
                (slow.sequencer.state_of(chat) or {}).get("activeTurn") is None
                and not slow._sessions[uri].running(chat)
            ),
            timeout=10.0,
        )

        assert _state(slow, chat).get("activeTurn") is None
        # Cancelled and cleared: `_cancel_turn` pops the chat's slot, so the
        # session no longer counts it as running.
        assert not slow._sessions[uri].running(chat)

    async def test_a_provider_that_cannot_forget_is_refused(self) -> None:
        """A visible refusal beats a silent lie. Stricter than the spec, which
        gates `chat/truncated` on nothing -- and deliberately so."""

        class Amnesiac:
            """The smallest complete session, with no truncation."""

            def __init__(self, context: AgentSessionContext) -> None:
                self.context = context

            async def send_user_message(self, message: Any, sink: Any) -> None:
                await sink.text_delta(message.text)

            async def cancel(self, reason: str | None = None) -> None: ...

            async def aclose(self) -> None: ...

        class Plain(EchoProvider):
            async def create_session(self, context: AgentSessionContext) -> Any:
                return Amnesiac(context)

        host = Host(Plain(), LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            uri = "echo:/x-5"
            chat = await _session(host, client, uri)
            await _turn(client, chat, text="hello")
            await _ran(host, client, chat, "t1")
            client.notifications.clear()

            await self._truncate(client, chat, 2, turnId="t1")
            # Fixed: the second assertion is that the turns were NOT dropped,
            # and stopping at the refusal would not give a host that refuses
            # and then truncates anyway the chance to be caught.
            await client.collect(seconds=0.5)

            rejected = [
                note
                for note in client.notifications
                if note.get("method") == "action"
                and note["params"].get("rejectionReason") is not None
            ]
            assert rejected, "the refusal was not echoed, so the client cannot revert"
            assert _state(host, chat)["turns"], "turns were dropped anyway"
        finally:
            await host.aclose()


async def _create_chat(client: FakeClient, session: str, chat: str) -> None:
    response = await client.request("createChat", {"channel": session, "chat": chat})
    assert "error" not in response, response


def _summary_changes(client: FakeClient, session: str) -> list[dict[str, Any]]:
    return [
        n["params"]["changes"]
        for n in client.notifications
        if n.get("method") == "root/sessionSummaryChanged" and n["params"]["session"] == session
    ]


class TestCompactCatalogue:
    """`SessionSummary.chats` / `defaultChat` and per-chat read state (1.0.0)."""

    async def test_list_sessions_carries_the_ordered_compact_catalogue(self, host: Host) -> None:
        client = await _client(host)
        default = await _session(host, client, "echo:/compact-1")
        await _create_chat(client, "echo:/compact-1", "ahp-chat:/compact-1-b")
        await client.collect(seconds=0.2)
        listing = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]
        summary = next(i for i in listing["items"] if i["resource"] == "echo:/compact-1")
        assert summary["defaultChat"] == default
        assert [c["resource"] for c in summary["chats"]] == [default, "ahp-chat:/compact-1-b"]
        for entry in summary["chats"]:
            assert set(entry) <= {
                "resource",
                "title",
                "origin",
                "interactivity",
                "status",
                "changes",
            }
            assert isinstance(entry["title"], str)

    async def test_chat_read_state_reaches_the_catalogue_and_the_root(self, host: Host) -> None:
        client = await _client(host)
        session = "echo:/compact-2"
        await _session(host, client, session)
        side = "ahp-chat:/compact-2-b"
        await _create_chat(client, session, side)
        await client.request("subscribe", {"channel": side})
        await client.notify(
            "dispatchAction",
            {
                "channel": side,
                "clientSeq": 7,
                "action": {"type": "chat/isReadChanged", "isRead": True},
            },
        )

        def compact_read() -> bool:
            for changes in _summary_changes(client, session):
                for entry in changes.get("chats", []):
                    if entry["resource"] == side and entry.get("status", 0) & SessionStatus.IS_READ:
                        return True
            return False

        await client.collect_until(compact_read, timeout=5.0)
        assert compact_read(), "root/sessionSummaryChanged never carried the chat's read bit"
        assert _catalogue(host, session, side)["status"] & SessionStatus.IS_READ
        # Scoped to the addressed chat: the session's own read state is untouched.
        assert not _state(host, session)["status"] & SessionStatus.IS_READ
