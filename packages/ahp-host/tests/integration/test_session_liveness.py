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
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair
from ahp_protocol.types.protocol import SessionStatus

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.host import _promotion_rank, _title_from
from ahp_host.provider import EchoProvider
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


class _SelfNarrating(EchoSession):
    """Sets an activity itself, the way `writing-a-provider.md` says to.

    Outside a tool call, so the sink never publishes the string and never sees
    it published -- which is the whole point: the provider and the sink are two
    writers of `session/activityChanged`.
    """

    async def send_user_message(self, message: Any, sink: Any) -> None:
        await sink.text_delta("done")
        assert self.context.publisher is not None
        await self.context.publisher.activity_changed("Editing core.py")


class _SelfNarratingProvider(EchoProvider):
    async def create_session(self, context: Any) -> _SelfNarrating:
        return _SelfNarrating(context)


@pytest.fixture
async def narrating() -> AsyncIterator[Host]:
    made = Host(_SelfNarratingProvider(), LoopbackSingleUserPolicy())
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
    # Bring-up runs AFTER the create response, and it is what registers the
    # default chat and publishes `session/ready`. Waited for rather than slept
    # through: the line below indexes `chats[0]`, so a wait that ends early is
    # an IndexError rather than a diagnosis.
    await client.collect_until(lambda: _is_ready(host, uri), timeout=10.0)
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
    host: Host, client: FakeClient, session: str, default: str, name: str, *, seed: str = "seed"
) -> str:
    """Open a side chat off a real turn in the default chat.

    `createChat` refuses a source turn that does not exist, so a test that
    skips the seeding turn gets an error response, no channel, and then passes
    for the wrong reason -- every later dispatch to the URI is simply dropped.
    """
    await _turn(client, default, text=seed, turn="seed-turn")
    # Waited for, not slept through: `createChat` refuses a source turn that
    # has not landed yet, and how long a turn takes is the fixture's business.
    await _ran(host, client, default, "seed-turn")
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


def _at(host: Host, uri: str) -> dict[str, Any]:
    """*uri*'s state, or empty. `_state` asserts; a wait predicate must not."""
    state = host.sequencer.state_of(uri)
    return state if isinstance(state, dict) else {}


def _is_ready(host: Host, uri: str) -> bool:
    """The session has finished coming up and has its default chat listed."""
    state = _at(host, uri)
    return state.get("lifecycle") == "ready" and bool(state.get("chats"))


def _summary_status(host: Host, uri: str) -> int:
    status = host._full_summary(host._sessions[uri])["status"]
    assert isinstance(status, int)
    return status


async def _ran(
    host: Host, client: FakeClient, chat: str, turn: str, *, timeout: float = 10.0
) -> None:
    """Wait for *turn* to land in *chat*'s completed list.

    `chat/turnStarted` only sets `activeTurn`; a turn reaches `turns` when it
    completes, so this is the turn genuinely being OVER rather than begun.
    """
    await client.collect_until(
        lambda: any(t.get("id") == turn for t in _at(host, chat).get("turns", [])),
        timeout=timeout,
    )


async def _titled(
    host: Host, client: FakeClient, uri: str, title: str, *, timeout: float = 10.0
) -> None:
    """Wait for *uri*'s published title to be *title*."""
    await client.collect_until(lambda: _at(host, uri).get("title") == title, timeout=timeout)


async def _no_activity(host: Host, client: FakeClient, uri: str, *, timeout: float = 10.0) -> None:
    """Wait for *uri* to stop advertising an activity.

    Clearing it is the LAST thing a turn does and it happens in a detached
    task, so the turn being over does not imply this yet -- the absence of the
    key is the only honest thing to wait on.
    """
    await client.collect_until(lambda: "activity" not in _at(host, uri), timeout=timeout)


def _ready_call(client: FakeClient, chat: str) -> str | None:
    """The id of the tool call *chat* announced, if one has been announced."""
    for note in client.notifications:
        if note.get("method") != "action":
            continue
        envelope = note["params"]
        if envelope.get("channel") != chat:
            continue
        if envelope["action"].get("type") == "chat/toolCallReady":
            call_id: str = envelope["action"]["toolCallId"]
            return call_id
    return None


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
        await _titled(host, client, uri, "add a retry to the fetch helper")

        assert _state(host, uri)["title"] == "add a retry to the fetch helper"

    async def test_the_second_message_does_not_rename_it(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/t-2"
        chat = await _session(host, client, uri)

        await _turn(client, chat, text="first", turn="t1")
        await _titled(host, client, uri, "first")
        await _turn(client, chat, text="second", turn="t2")
        # The rename, if it happened, would happen at the START of `t2`: the
        # host seeds the title before it creates the turn task. So `t2` being
        # over is strictly past the moment this test is watching for.
        await _ran(host, client, chat, "t2")

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
        await _titled(host, client, uri, "Refactor")

        await _turn(client, chat, text="do the thing")
        await _ran(host, client, chat, "t1")

        assert _state(host, uri)["title"] == "Refactor"

    async def test_a_side_chat_does_not_name_the_session(self, host: Host) -> None:
        """A side chat asks about a selection -- "what does this do?" is a
        terrible name for the session it was asked in."""
        client = await _client(host)
        uri = "echo:/t-4"
        default = await _session(host, client, uri)
        aside = await _side_chat(
            host, client, uri, default, "ahp-chat:/aside", seed="build the parser"
        )
        assert _state(host, uri)["title"] == "build the parser"

        await _turn(client, aside, text="what does this do?", turn="aside-1")
        await _ran(host, client, aside, "aside-1")

        assert _state(host, uri)["title"] == "build the parser"

    async def test_an_empty_message_leaves_the_default(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/t-5"
        chat = await _session(host, client, uri)

        await _turn(client, chat, text="   \n  ")
        await _ran(host, client, chat, "t1")

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

    def _ready(self, client: FakeClient, chat: str) -> str:
        call_id = _ready_call(client, chat)
        assert call_id is not None, "no tool call was ever announced"
        return call_id

    async def test_a_running_tool_names_itself(self, tooled: Host) -> None:
        client = await _client(tooled)
        uri = "echo:/a-1"
        chat = await _session(tooled, client, uri)
        await client.request("subscribe", {"channel": chat})

        await _turn(client, chat, text="hello")
        # BOTH, because the confirmation below needs the announced call id and
        # stopping at the activity alone could leave that notification still in
        # the transport -- "no tool call was ever announced" for a call that
        # simply had not been read yet.
        await client.collect_until(
            lambda: (
                _at(tooled, uri).get("activity") == "Echo Tool"
                and _ready_call(client, chat) is not None
            ),
            timeout=10.0,
        )

        # The turn is parked on the confirmation, so the tool is genuinely
        # running right now.
        assert _state(tooled, uri).get("activity") == "Echo Tool"

        await self._confirm(client, chat, self._ready(client, chat))
        await _no_activity(tooled, client, uri)
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
        await client.collect_until(
            lambda: any(
                changes.get("activity") == "Echo Tool" for changes in _summaries(client, uri)
            ),
            timeout=10.0,
        )

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
        await client.collect_until(
            lambda: _at(tooled, uri).get("activity") == "Echo Tool", timeout=10.0
        )
        assert _state(tooled, uri).get("activity") == "Echo Tool"

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 2,
                "action": {"type": "chat/turnCancelled", "turnId": "t1"},
            },
        )
        await _no_activity(tooled, client, uri)

        assert "activity" not in _state(tooled, uri)

    async def test_a_provider_set_activity_is_retracted_too(self, narrating: Host) -> None:
        """The one the sink never published, and so believed was not there.

        `session/activityChanged` has two writers: this sink, and
        `SessionPublisher.activity_changed`, which a provider may call at any
        time -- the guide tells it to, in those words. The sink deduped against
        a cache only it wrote, so a string it had not published looked like no
        string at all, and the `set_activity(None)` in the turn's `finally`
        deduped itself away. The session went idle still claiming to be editing
        a file, and only a later tool call could ever clear it.
        """
        client = await _client(narrating)
        uri = "echo:/a-4"
        chat = await _session(narrating, client, uri)
        await client.request("subscribe", {"channel": chat})

        await _turn(client, chat, text="hello")
        await client.collect_until(
            lambda: _at(narrating, uri).get("activity") == "Editing core.py", timeout=10.0
        )
        assert _state(narrating, uri).get("activity") == "Editing core.py"

        await _no_activity(narrating, client, uri)
        assert "activity" not in _state(narrating, uri), "the activity outlived the turn"
        assert _summary_status(narrating, uri) == SessionStatus.IDLE


class TestUnread:
    """`session/isReadChanged` is a two-party protocol and we shipped one half."""

    async def _read(self, host: Host, client: FakeClient, uri: str, seq: int) -> None:
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": seq,
                "action": {"type": "session/isReadChanged", "isRead": True},
            },
        )
        await client.collect_until(
            lambda: bool(_at(host, uri).get("status", 0) & SessionStatus.IS_READ), timeout=10.0
        )

    async def test_an_answer_makes_a_read_session_unread_again(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/u-1"
        chat = await _session(host, client, uri)
        await client.request("subscribe", {"channel": uri})
        await self._read(host, client, uri, 1)
        assert _state(host, uri)["status"] & SessionStatus.IS_READ

        await _turn(client, chat, text="hello")
        # The dot comes back from a DETACHED task after the turn, so the turn
        # being over does not imply it yet -- this waits for the bit itself.
        await client.collect_until(
            lambda: not _at(host, uri).get("status", 0) & SessionStatus.IS_READ, timeout=10.0
        )

        assert not _state(host, uri)["status"] & SessionStatus.IS_READ

    async def test_not_while_a_client_is_looking_at_it(self, host: Host) -> None:
        """A badge on the session currently on screen is noise."""
        client = await _client(host)
        uri = "echo:/u-2"
        chat = await _session(host, client, uri)
        await client.request("subscribe", {"channel": uri})
        await self._read(host, client, uri, 1)
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 2,
                "action": {"type": "session/activeClientSet", "clientId": "c1"},
            },
        )
        # The whole point of the test is that somebody is WATCHING, and that is
        # `activeClients`. Racing the turn against this dispatch would test the
        # other branch by accident.
        await client.collect_until(lambda: bool(_at(host, uri).get("activeClients")), timeout=10.0)

        await _turn(client, chat, text="hello")
        # Fixed on purpose. The claim is that nothing arrived -- `_mark_unread`
        # runs detached after the turn and DECLINES -- and a condition wait on
        # a bit that is already set returns instantly and proves nothing.
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
        # Fixed on purpose, same reason: "one action per turn that was already
        # unread is pure noise" is a claim about what was NOT published, and
        # `_mark_unread` is detached, so only elapsed time can support it.
        await client.collect(seconds=0.8)

        assert not any("isRead" in str(changes) for changes in _summaries(client, uri))


class TestSideChatPromotion:
    """A session list renders one row per session, so a session whose only
    working chat is not the default one reads as finished when it is not."""

    async def test_a_working_side_chat_shows_in_the_summary(self, slow: Host) -> None:
        client = await _client(slow)
        uri = "echo:/p-1"
        default = await _session(slow, client, uri)
        worker = await _side_chat(slow, client, uri, default, "ahp-chat:/worker")

        await _turn(client, worker, text="hello there, this takes a while", turn="w1")
        # Sampled at the first moment the summary claims to be working, not at
        # a fixed 0.25s -- the turn takes 0.3s, so the old sample was one loaded
        # runner away from landing after the turn it was trying to observe.
        # Both assertions read the SAME sample: "Idle and InProgress at once"
        # is only a statement about one snapshot.
        await client.collect_until(
            lambda: bool(_summary_status(slow, uri) & SessionStatus.IN_PROGRESS), timeout=10.0
        )
        status = _summary_status(slow, uri)

        await _ran(slow, client, worker, "w1")
        assert status & SessionStatus.IN_PROGRESS, "a working side chat left the session Idle"
        assert not status & SessionStatus.IDLE, "Idle and InProgress at once"

    async def test_it_goes_away_when_the_side_chat_finishes(self, slow: Host) -> None:
        client = await _client(slow)
        uri = "echo:/p-3"
        default = await _session(slow, client, uri)
        worker = await _side_chat(slow, client, uri, default, "ahp-chat:/worker")

        await _turn(client, worker, text="hi", turn="w1")
        # The turn having RUN is half the condition and it is the half that
        # keeps this honest: a bare wait for "not in progress" is satisfied
        # before the turn is picked up at all, and would pass on a host that
        # never promotes a side chat in the first place.
        await client.collect_until(
            lambda: (
                any(t.get("id") == "w1" for t in _at(slow, worker).get("turns", []))
                and not _summary_status(slow, uri) & SessionStatus.IN_PROGRESS
            ),
            timeout=10.0,
        )

        status = _summary_status(slow, uri)
        assert not status & SessionStatus.IN_PROGRESS

    async def test_needing_input_outranks_merely_working(self, slow: Host) -> None:
        """The chats after the default are walked in URI order, so first-wins
        would hand the activity string to whichever chat sorted earliest -- and
        "a-busy" sorts before "z-blocked" precisely to catch that."""
        client = await _client(slow)
        uri = "echo:/p-2"
        default = await _session(slow, client, uri)
        busy = await _side_chat(slow, client, uri, default, "ahp-chat:/a-busy")
        blocked = await _side_chat(slow, client, uri, default, "ahp-chat:/z-blocked")

        # Both really running -- waited for rather than assumed after 0.1s,
        # because a chat's status is DERIVED from its active turn and the
        # publishes below say nothing about a chat that has not started one.
        await _turn(client, busy, text="hello there, this takes a while", turn="b1")
        await _turn(client, blocked, text="hello there, this takes a while", turn="z1")
        await client.collect_until(
            lambda: (
                _at(slow, busy).get("activeTurn") is not None
                and _at(slow, blocked).get("activeTurn") is not None
            ),
            timeout=10.0,
        )
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

        # Let both turns finish rather than tearing the host down through them.
        await _ran(slow, client, busy, "b1")
        await _ran(slow, client, blocked, "z1")


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
