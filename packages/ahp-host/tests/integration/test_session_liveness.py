"""What the session list shows while work is happening.

Every session was called "New Session", nothing published `activity`, the unread
dot never came back, and a working side chat left the session reading `Idle`. So
a list of three sessions was three identical rows, and none of them moved.

None of that is a protocol violation -- it is all `SHOULD`-shaped, which is
exactly why nothing caught it. These tests are the enforcement.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair
from agent_host_protocol.types.protocol import SessionStatus

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.host import _promotion_rank, _title_from
from agent_host_server.provider import EchoProvider

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
    """A host whose turns stream slowly, so a chat is observably `InProgress`.

    An echo turn with no delay is in progress for microseconds, which is not
    long enough to look at.
    """
    made = Host(EchoProvider(capabilities=_FORKABLE, delay=0.3), LoopbackSingleUserPolicy())
    try:
        yield made
    finally:
        await made.aclose()


@pytest.fixture
async def tooled() -> AsyncIterator[Host]:
    """A host whose turns open a tool call and then WAIT for confirmation.

    The window a running tool leaves open is the thing being measured, and a
    turn that finishes in microseconds has no window to measure.
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


async def _session(host: Host, client: FakeClient, uri: str) -> str:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect(seconds=0.3)
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat: str = state["chats"][0]["resource"]
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


async def _side_chat(
    client: FakeClient, session: str, default: str, name: str, *, seed: str = "seed"
) -> str:
    """Open a side chat off a real turn in the default chat.

    `createChat` refuses a source turn that does not exist, so a test that
    skips the seeding turn gets an error response, no channel, and then passes
    for the wrong reason -- every later dispatch to the URI is simply dropped.
    """
    await _turn(client, default, text=seed, turn="seed-turn")
    # Waited for, not slept through: `createChat` refuses a source turn that
    # has not landed yet, and how long a turn takes is the fixture's business.
    for _ in range(40):
        await client.collect(seconds=0.1)
        state = (await client.request("subscribe", {"channel": default}))["result"]["snapshot"][
            "state"
        ]
        if any(turn.get("id") == "seed-turn" for turn in state.get("turns", [])):
            break
    result = await client.request(
        "createChat",
        {
            "channel": session,
            "chat": name,
            "source": {"kind": "sideChat", "chat": default, "turnId": "seed-turn"},
        },
    )
    assert "error" not in result, result
    await client.request("subscribe", {"channel": name})
    return name


def _state(host: Host, uri: str) -> dict[str, Any]:
    state = host.sequencer.state_of(uri)
    assert isinstance(state, dict)
    return state


def _summaries(client: FakeClient, session: str) -> list[dict[str, Any]]:
    """Every `root/sessionSummaryChanged` this client saw for *session*."""
    return [
        note["params"]["changes"]
        for note in client.notifications
        if note.get("method") == "root/sessionSummaryChanged"
        and note["params"].get("session") == session
    ]


class TestTitleFromTheFirstMessage:
    """A list of sessions all called "New Session" is a list of one thing."""

    async def test_the_first_message_names_the_session(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/t-1"
        chat = await _session(host, client, uri)
        assert _state(host, uri)["title"] == "New Session"

        await _turn(client, chat, text="add a retry to the fetch helper")
        await client.collect(seconds=0.5)

        assert _state(host, uri)["title"] == "add a retry to the fetch helper"

    async def test_the_second_message_does_not_rename_it(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/t-2"
        chat = await _session(host, client, uri)

        await _turn(client, chat, text="first", turn="t1")
        await client.collect(seconds=0.5)
        await _turn(client, chat, text="second", turn="t2")
        await client.collect(seconds=0.5)

        assert _state(host, uri)["title"] == "first"

    async def test_a_client_rename_survives_the_next_turn(self, host: Host) -> None:
        """`session/titleChanged` is client-dispatchable. Overwriting a rename
        would make the rename look like it silently failed."""
        client = await _client(host)
        uri = "echo:/t-3"
        chat = await _session(host, client, uri)
        await client.request("subscribe", {"channel": uri})
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {"type": "session/titleChanged", "title": "Refactor"},
            },
        )
        await client.collect(seconds=0.3)

        await _turn(client, chat, text="do the thing")
        await client.collect(seconds=0.5)

        assert _state(host, uri)["title"] == "Refactor"

    async def test_a_side_chat_does_not_name_the_session(self, host: Host) -> None:
        """A side chat asks about a selection -- "what does this do?" is a
        terrible name for the session it was asked in."""
        client = await _client(host)
        uri = "echo:/t-4"
        default = await _session(host, client, uri)
        aside = await _side_chat(client, uri, default, "ahp-chat:/aside", seed="build the parser")
        assert _state(host, uri)["title"] == "build the parser"

        await _turn(client, aside, text="what does this do?", turn="aside-1")
        await client.collect(seconds=0.5)

        assert _state(host, uri)["title"] == "build the parser"

    async def test_an_empty_message_leaves_the_default(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/t-5"
        chat = await _session(host, client, uri)

        await _turn(client, chat, text="   \n  ")
        await client.collect(seconds=0.5)

        assert _state(host, uri)["title"] == "New Session"


class TestTitleDerivation:
    """`_title_from` on its own, where the edges are cheap to state."""

    def test_nothing_from_nothing(self) -> None:
        assert _title_from("") is None
        assert _title_from("   \n\t ") is None

    def test_the_first_line_only(self) -> None:
        assert _title_from("fix the parser\n\nit crashes on empty input") == "fix the parser"

    def test_leading_blank_lines_are_skipped(self) -> None:
        assert _title_from("\n\n  hello  ") == "hello"

    def test_whitespace_is_collapsed(self) -> None:
        assert _title_from("too    many\tspaces") == "too many spaces"

    def test_a_long_message_is_cut_at_a_word(self) -> None:
        title = _title_from("please " * 40)
        assert title is not None
        assert title.endswith("…")
        assert "  " not in title
        # Cut on a boundary, so the last word is whole.
        assert title.rstrip("…").split()[-1] == "please"

    def test_one_enormous_word_is_cut_anyway(self) -> None:
        """No boundary to find. A hard cut beats returning the whole thing."""
        title = _title_from("x" * 500)
        assert title is not None
        assert len(title) < 100


class TestActivity:
    """ "Working..." is the client's own fallback, and it is the same string for
    every session in the list."""

    async def _confirm(self, client: FakeClient, chat: str, call_id: str) -> None:
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 3,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": call_id,
                    "approved": True,
                    "confirmed": "user-action",
                },
            },
        )
        await client.collect(seconds=0.5)

    def _ready(self, client: FakeClient, chat: str) -> str:
        for note in client.notifications:
            if note.get("method") != "action":
                continue
            envelope = note["params"]
            if envelope.get("channel") != chat:
                continue
            if envelope["action"].get("type") == "chat/toolCallReady":
                call_id: str = envelope["action"]["toolCallId"]
                return call_id
        raise AssertionError("no tool call was ever announced")

    async def test_a_running_tool_names_itself(self, tooled: Host) -> None:
        client = await _client(tooled)
        uri = "echo:/a-1"
        chat = await _session(tooled, client, uri)
        await client.request("subscribe", {"channel": chat})

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.5)

        # The turn is parked on the confirmation, so the tool is genuinely
        # running right now.
        assert _state(tooled, uri).get("activity") == "Echo Tool"

        await self._confirm(client, chat, self._ready(client, chat))
        assert "activity" not in _state(tooled, uri), "activity outlived the turn"

    async def test_the_summary_moves_with_it(self, tooled: Host) -> None:
        """The activity only renders in the session list, which is fed by
        `root/sessionSummaryChanged` -- publishing it to the session channel
        alone would change nothing a user can see."""
        client = await _client(tooled)
        uri = "echo:/a-2"
        chat = await _session(tooled, client, uri)
        await client.request("subscribe", {"channel": chat})
        client.notifications.clear()

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.5)

        assert any(changes.get("activity") == "Echo Tool" for changes in _summaries(client, uri)), (
            "the session list never saw it"
        )

    async def test_a_cancelled_turn_does_not_strand_it(self, tooled: Host) -> None:
        """The turn task is the one being cancelled, so whatever clears the
        activity has to survive that."""
        client = await _client(tooled)
        uri = "echo:/a-3"
        chat = await _session(tooled, client, uri)
        await client.request("subscribe", {"channel": chat})

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.5)
        assert _state(tooled, uri).get("activity") == "Echo Tool"

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 2,
                "action": {"type": "chat/turnCancelled", "turnId": "t1"},
            },
        )
        await client.collect(seconds=0.6)

        assert "activity" not in _state(tooled, uri)


class TestUnread:
    """`session/isReadChanged` is a two-party protocol and we shipped one half."""

    async def _read(self, client: FakeClient, uri: str, seq: int) -> None:
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": seq,
                "action": {"type": "session/isReadChanged", "isRead": True},
            },
        )
        await client.collect(seconds=0.3)

    async def test_an_answer_makes_a_read_session_unread_again(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/u-1"
        chat = await _session(host, client, uri)
        await client.request("subscribe", {"channel": uri})
        await self._read(client, uri, 1)
        assert _state(host, uri)["status"] & SessionStatus.IS_READ

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.8)

        assert not _state(host, uri)["status"] & SessionStatus.IS_READ

    async def test_not_while_a_client_is_looking_at_it(self, host: Host) -> None:
        """A badge on the session currently on screen is noise."""
        client = await _client(host)
        uri = "echo:/u-2"
        chat = await _session(host, client, uri)
        await client.request("subscribe", {"channel": uri})
        await self._read(client, uri, 1)
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 2,
                "action": {"type": "session/activeClientSet", "clientId": "c1"},
            },
        )
        await client.collect(seconds=0.3)

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.8)

        assert _state(host, uri)["status"] & SessionStatus.IS_READ

    async def test_a_never_read_session_is_left_alone(self, host: Host) -> None:
        """Nothing to clear, so nothing is published -- one action per turn on
        every session that was already unread is pure noise."""
        client = await _client(host)
        uri = "echo:/u-3"
        chat = await _session(host, client, uri)
        assert not _state(host, uri)["status"] & SessionStatus.IS_READ
        client.notifications.clear()

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.8)

        assert not any("isRead" in str(changes) for changes in _summaries(client, uri))


class TestSideChatPromotion:
    """A session list renders one row per session, so a session whose only
    working chat is not the default one reads as finished when it is not."""

    async def test_a_working_side_chat_shows_in_the_summary(self, slow: Host) -> None:
        client = await _client(slow)
        uri = "echo:/p-1"
        default = await _session(slow, client, uri)
        worker = await _side_chat(client, uri, default, "ahp-chat:/worker")

        await _turn(client, worker, text="hello there, this takes a while", turn="w1")
        await asyncio.sleep(0.25)
        status = slow._full_summary(slow._sessions[uri])["status"]

        await client.collect(seconds=1.2)
        assert status & SessionStatus.IN_PROGRESS, "a working side chat left the session Idle"
        assert not status & SessionStatus.IDLE, "Idle and InProgress at once"

    async def test_it_goes_away_when_the_side_chat_finishes(self, slow: Host) -> None:
        client = await _client(slow)
        uri = "echo:/p-3"
        default = await _session(slow, client, uri)
        worker = await _side_chat(client, uri, default, "ahp-chat:/worker")

        await _turn(client, worker, text="hi", turn="w1")
        await client.collect(seconds=1.5)

        status = slow._full_summary(slow._sessions[uri])["status"]
        assert not status & SessionStatus.IN_PROGRESS

    async def test_needing_input_outranks_merely_working(self, slow: Host) -> None:
        """The chats after the default are walked in URI order, so first-wins
        would hand the activity string to whichever chat sorted earliest -- and
        "a-busy" sorts before "z-blocked" precisely to catch that."""
        client = await _client(slow)
        uri = "echo:/p-2"
        default = await _session(slow, client, uri)
        busy = await _side_chat(client, uri, default, "ahp-chat:/a-busy")
        blocked = await _side_chat(client, uri, default, "ahp-chat:/z-blocked")

        # Both really running...
        await _turn(client, busy, text="hello there, this takes a while", turn="b1")
        await _turn(client, blocked, text="hello there, this takes a while", turn="z1")
        await asyncio.sleep(0.1)
        # ...and one of them really waiting on a human. Published exactly as the
        # host publishes it when a provider parks, rather than written into
        # state -- the chat's status is DERIVED from its active turn, so a
        # request against a turn that is not running changes nothing.
        await slow.sequencer.publish(
            blocked,
            {
                "type": "chat/inputRequested",
                "turnId": "z1",
                "request": {"id": "r1", "questions": [{"kind": "freeform", "prompt": "which?"}]},
            },
        )
        for chat, activity in ((busy, "busy"), (blocked, "blocked")):
            await slow.sequencer.publish(
                chat, {"type": "chat/activityChanged", "activity": activity}
            )

        summary = slow._full_summary(slow._sessions[uri])
        assert summary["status"] & SessionStatus.INPUT_NEEDED == SessionStatus.INPUT_NEEDED
        assert summary["activity"] == "blocked", "the busier-but-earlier chat won"

        await client.collect(seconds=1.5)


class TestPromotionRank:
    """The ordering on its own. `InputNeeded` shares a bit with `InProgress`,
    so getting this wrong reads every blocked chat as merely busy."""

    def test_the_order(self) -> None:
        assert _promotion_rank(SessionStatus.INPUT_NEEDED) > _promotion_rank(SessionStatus.ERROR)
        assert _promotion_rank(SessionStatus.ERROR) > _promotion_rank(SessionStatus.IN_PROGRESS)
        assert _promotion_rank(SessionStatus.IN_PROGRESS) > 0

    def test_idle_does_not_promote(self) -> None:
        assert _promotion_rank(SessionStatus.IDLE) == 0
        assert _promotion_rank(0) == 0

    def test_input_needed_is_not_mistaken_for_in_progress(self) -> None:
        """It is `(1 << 3) | (1 << 4)`, so a bare `& IN_PROGRESS` matches it."""
        assert SessionStatus.INPUT_NEEDED & SessionStatus.IN_PROGRESS
        assert _promotion_rank(SessionStatus.INPUT_NEEDED) != _promotion_rank(
            SessionStatus.IN_PROGRESS
        )
