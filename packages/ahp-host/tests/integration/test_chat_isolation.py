"""Four ways one chat could reach into another, all found by the Python client.

Every one of these needed a *second* chat to see. The whole suite ran against a
single chat per session, so a slot that should have been per-chat looked like a
slot, an id minted globally looked scoped, and a stub action looked like the
action it stood in for. Driving two independently-built implementations against
each other is what surfaced them; these tests are what keep them shut.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.resources import RootedFilesystemResourceProvider
from agent_host_server.core.watches import PollingResourceWatcher
from agent_host_server.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_FORKABLE = {"multipleChats": {"fork": True, "sideChat": True}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def slow() -> AsyncIterator[Host]:
    """Turns slow enough that two can genuinely overlap."""
    made = Host(EchoProvider(capabilities=_FORKABLE, delay=0.4), LoopbackSingleUserPolicy())
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
    await client.collect(seconds=0.3)
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat: str = state["chats"][0]["resource"]
    await client.request("subscribe", {"channel": chat})
    return chat


async def _turn(client: FakeClient, chat: str, *, text: str, turn: str, seq: int = 1) -> None:
    await client.notify(
        "dispatchAction",
        {
            "channel": chat,
            "clientSeq": seq,
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
    assert isinstance(state, dict)
    return state


async def _settled(host: Host, client: FakeClient, chat: str, turn: str) -> None:
    """Wait for *turn* to reach the chat's completed list.

    Named rather than slept through: `createChat` refuses a source turn that has
    not landed, so the fixed wait this replaces was a race whose failure mode
    was an error response the caller then asserted past.
    """
    await client.collect_until(
        lambda: any(
            t.get("id") == turn for t in (host.sequencer.state_of(chat) or {}).get("turns", [])
        ),
        timeout=10.0,
    )


async def _idle(host: Host, client: FakeClient, chat: str, *, timeout: float = 10.0) -> None:
    """Wait for *chat* to have no active turn."""
    await client.collect_until(
        lambda: (host.sequencer.state_of(chat) or {}).get("activeTurn") is None,
        timeout=timeout,
    )


async def _ran(
    host: Host, client: FakeClient, chat: str, turn: str, *, timeout: float = 10.0
) -> None:
    """Wait for *turn* to appear in *chat*'s completed list."""
    await client.collect_until(
        lambda: any(
            t.get("id") == turn for t in (host.sequencer.state_of(chat) or {}).get("turns", [])
        ),
        timeout=timeout,
    )


def _rejections(client: FakeClient) -> list[str]:
    return [
        note["params"]["rejectionReason"]
        for note in client.notifications
        if note.get("method") == "action" and note["params"].get("rejectionReason")
    ]


class TestCancellingOneChatLeavesTheOtherAlone:
    """`_Session.turn` was ONE slot for the whole session, overwritten per chat,
    so a cancel killed whichever chat had started most recently -- with no
    terminal action on the victim, which was then pinned at `activeTurn` forever
    and rejected every later turn as "a turn is already active"."""

    async def _two_chats(self, host: Host, client: FakeClient, uri: str) -> tuple[str, str]:
        default = await _session(host, client, uri)
        await _turn(client, default, text="seed", turn="seed-turn")
        await _settled(host, client, default, "seed-turn")
        result = await client.request(
            "createChat",
            {
                "channel": uri,
                "chat": "ahp-chat:/second",
                "source": {"kind": "sideChat", "chat": default, "turnId": "seed-turn"},
            },
        )
        assert "error" not in result, result
        await client.request("subscribe", {"channel": "ahp-chat:/second"})
        return default, "ahp-chat:/second"

    async def test_the_innocent_chat_finishes_its_turn(self, slow: Host) -> None:
        client = await _client(slow)
        uri = "echo:/iso-1"
        a, b = await self._two_chats(slow, client, uri)

        # B starts LAST, so under the old single-slot behaviour B held the only
        # handle and cancelling A destroyed B.
        await _turn(client, a, text="for A", turn="turn-A", seq=10)
        await asyncio.sleep(0.15)
        await _turn(client, b, text="for B", turn="turn-B", seq=11)
        await asyncio.sleep(0.15)

        await client.notify(
            "dispatchAction",
            {
                "channel": a,
                "clientSeq": 12,
                "action": {"type": "chat/turnCancelled", "turnId": "turn-A"},
            },
        )
        await _idle(slow, client, b)

        assert _state(slow, b).get("activeTurn") is None, "B never finished"
        assert [t["id"] for t in _state(slow, b)["turns"]] == ["turn-B"]
        assert _state(slow, b)["turns"][0]["state"] == "complete"

    async def test_the_innocent_chat_still_accepts_work(self, slow: Host) -> None:
        """The lasting damage: B was left claiming a turn was running, so every
        later `chat/turnStarted` was rejected. Unrecoverable short of disposal."""
        client = await _client(slow)
        uri = "echo:/iso-2"
        a, b = await self._two_chats(slow, client, uri)

        await _turn(client, a, text="for A", turn="turn-A", seq=10)
        await asyncio.sleep(0.15)
        await _turn(client, b, text="for B", turn="turn-B", seq=11)
        await asyncio.sleep(0.15)
        await client.notify(
            "dispatchAction",
            {
                "channel": a,
                "clientSeq": 12,
                "action": {"type": "chat/turnCancelled", "turnId": "turn-A"},
            },
        )
        await _idle(slow, client, b)
        client.notifications.clear()

        await _turn(client, b, text="again", turn="turn-B2", seq=13)
        await _ran(slow, client, b, "turn-B2")

        assert not _rejections(client), "B was bricked"
        assert "turn-B2" in [t["id"] for t in _state(slow, b)["turns"]]

    async def test_the_cancelled_chat_is_the_one_that_stops(self, slow: Host) -> None:
        client = await _client(slow)
        uri = "echo:/iso-3"
        a, b = await self._two_chats(slow, client, uri)

        await _turn(client, a, text="for A", turn="turn-A", seq=10)
        await asyncio.sleep(0.15)
        await _turn(client, b, text="for B", turn="turn-B", seq=11)
        await asyncio.sleep(0.15)
        await client.notify(
            "dispatchAction",
            {
                "channel": a,
                "clientSeq": 12,
                "action": {"type": "chat/turnCancelled", "turnId": "turn-A"},
            },
        )
        await _idle(slow, client, a)

        assert _state(slow, a)["turns"][-1]["state"] == "cancelled"

    async def test_the_counter_sees_both(self, slow: Host) -> None:
        """`activeTurns` counted one slot per session, so two chats working at
        once reported one -- and a liveness number that undercounts is worse
        than none."""
        client = await _client(slow)
        uri = "echo:/iso-4"
        a, b = await self._two_chats(slow, client, uri)

        await _turn(client, a, text="for A", turn="turn-A", seq=10)
        await _turn(client, b, text="for B", turn="turn-B", seq=11)
        await asyncio.sleep(0.2)
        during = slow.counters()["activeTurns"]

        await client.collect_until(lambda: slow.counters()["activeTurns"] == 0, timeout=10.0)
        assert during == 2, f"two chats were working; counted {during}"


class TestCreateChatWithAnInitialMessage:
    """`_create_chat` published a real `chat/turnStarted` and then kicked the
    turn with the stub `{"type": "chat/turnStarted"}`. `TurnRunner.run` reads
    `turnId` off what it is handed and returns on its first line when it is
    absent -- so the agent never saw the message and the chat sat at
    `activeTurn` forever. Wedged from birth, and `createChat` answered `{}`."""

    async def _fork(self, host: Host, client: FakeClient, uri: str, text: str) -> str:
        default = await _session(host, client, uri)
        await _turn(client, default, text="seed", turn="seed-turn")
        await _settled(host, client, default, "seed-turn")
        result = await client.request(
            "createChat",
            {
                "channel": uri,
                "chat": "ahp-chat:/with-message",
                "source": {"kind": "sideChat", "chat": default, "turnId": "seed-turn"},
                "initialMessage": {"text": text, "origin": {"kind": "user"}},
            },
        )
        assert "error" not in result, result
        await client.request("subscribe", {"channel": "ahp-chat:/with-message"})
        return "ahp-chat:/with-message"

    async def test_the_agent_actually_answers_it(self, slow: Host) -> None:
        client = await _client(slow)
        chat = await self._fork(slow, client, "echo:/init-1", "please answer this")
        await client.collect_until(
            lambda: bool((slow.sequencer.state_of(chat) or {}).get("turns")), timeout=10.0
        )

        turns = _state(slow, chat)["turns"]
        assert len(turns) == 1, f"the initial message never ran: {turns}"
        assert turns[0]["state"] == "complete"
        assert "please answer this" in str(turns[0]["responseParts"])

    async def test_the_chat_is_usable_afterwards(self, slow: Host) -> None:
        client = await _client(slow)
        chat = await self._fork(slow, client, "echo:/init-2", "first")
        await _idle(slow, client, chat)
        client.notifications.clear()

        await _turn(client, chat, text="second", turn="after", seq=20)
        await _ran(slow, client, chat, "after")

        assert not _rejections(client), "the chat was wedged from birth"
        assert "after" in [t["id"] for t in _state(slow, chat)["turns"]]


class TestOneChatCannotAnswerAnothersQuestion:
    """`PendingRequests` mints ids globally, and the gate checked only that the
    id was live. So dispatching `chat/inputCompleted` to the WRONG chat resolved
    the victim's future while reading answers out of the dispatched channel's
    state -- the answers went nowhere, the victim's transcript still said
    unanswered, and it stayed pinned in `InputNeeded` until disposal."""

    @pytest.fixture
    async def asking(self) -> AsyncIterator[Host]:
        made = Host(EchoProvider(capabilities=_FORKABLE, elicit=True), LoopbackSingleUserPolicy())
        try:
            yield made
        finally:
            await made.aclose()

    async def _parked(self, host: Host, client: FakeClient, uri: str) -> tuple[str, str]:
        """A session parked on an elicitation. Returns (chat, requestId)."""
        chat = await _session(host, client, uri)
        await _turn(client, chat, text="hello", turn="t1")
        for _ in range(40):
            await client.collect(seconds=0.1)
            needed = _state(host, uri).get("inputNeeded") or []
            if needed:
                request_id: str = needed[0]["id"]
                return chat, request_id
        raise AssertionError("the provider never parked")

    async def test_the_misdirected_answer_is_refused(self, asking: Host) -> None:
        client = await _client(asking)
        chat_a, request_a = await self._parked(asking, client, "echo:/ask-a")
        chat_b, request_b = await self._parked(asking, client, "echo:/ask-b")
        assert request_a != request_b
        client.notifications.clear()

        # A's request id, dispatched to B's chat.
        await client.notify(
            "dispatchAction",
            {
                "channel": chat_b,
                "clientSeq": 30,
                "action": {
                    "type": "chat/inputCompleted",
                    "requestId": request_a,
                    "response": "accept",
                    "answers": {"style": {"state": "submitted", "value": "hijacked"}},
                },
            },
        )
        await client.collect(seconds=0.6)

        assert _rejections(client) == ["no open input request with that id"]
        # Both are still waiting, each for its own question.
        assert [r["id"] for r in _state(asking, "echo:/ask-a")["inputNeeded"]] == [request_a]
        assert [r["id"] for r in _state(asking, "echo:/ask-b")["inputNeeded"]] == [request_b]

    async def test_the_right_chat_can_still_answer(self, asking: Host) -> None:
        """The gate must not be so tight that the legitimate answer bounces."""
        client = await _client(asking)
        chat, request_id = await self._parked(asking, client, "echo:/ask-c")
        client.notifications.clear()

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 30,
                "action": {
                    "type": "chat/inputCompleted",
                    "requestId": request_id,
                    "response": "accept",
                    "answers": {"style": {"state": "submitted", "value": "shout"}},
                },
            },
        )
        await client.collect(seconds=1.0)

        assert not _rejections(client)
        assert not _state(asking, "echo:/ask-c").get("inputNeeded")
        assert _state(asking, chat)["turns"][-1]["state"] == "complete"


class TestTheWatchStaysInTheJail:
    """`RootedFilesystemResourceProvider` lets a STRICT ANCESTOR of the served
    root resolve, so a directory picker can stat the parent of a path before the
    path itself. Every other surface then refuses it -- but `createResourceWatch`
    never applied `_inside_the_jail`, so a recursive watch on an ancestor (up to
    `file:///`) was accepted and the poller walked it, reporting names, existence
    and change timing for files the same peer is refused a read of.

    The provider's own docstring promises "reads, writes and watches stay
    refused". Two of three were true."""

    @pytest.fixture
    def jail(self) -> Any:
        with tempfile.TemporaryDirectory() as base:
            # `.resolve()` because the provider canonicalises its own root, and
            # on macOS a temp dir is `/var/...` symlinked to `/private/var/...`.
            # Without this the provider refuses its own root and the test fails
            # for a reason that has nothing to do with what it is testing.
            outside = Path(base).resolve()
            (outside / "secret.txt").write_text("not yours")
            root = outside / "served"
            root.mkdir()
            (root / "inside.txt").write_text("fine")
            yield outside, root

    async def _host(self, root: Path) -> Host:
        return Host(
            EchoProvider(),
            LoopbackSingleUserPolicy(),
            resources=RootedFilesystemResourceProvider(root),
            watcher=PollingResourceWatcher(),
        )

    async def test_a_watch_on_a_strict_ancestor_is_refused(self, jail: Any) -> None:
        outside, root = jail
        host = await self._host(root)
        try:
            client = await _client(host)
            response = await client.request(
                "createResourceWatch",
                {"channel": ROOT_URI, "uri": outside.as_uri(), "recursive": True},
            )
            assert "error" in response, f"the jail was escaped: {response}"
            assert response["error"]["code"] == -32009
        finally:
            await host.aclose()

    async def test_the_root_itself_still_watches(self, jail: Any) -> None:
        """The fix must not close the feature: the served root is the whole
        point of having a watcher."""
        _outside, root = jail
        host = await self._host(root)
        try:
            client = await _client(host)
            response = await client.request(
                "createResourceWatch",
                {"channel": ROOT_URI, "uri": root.as_uri(), "recursive": True},
            )
            assert "error" not in response, response
            assert response["result"]["channel"]
        finally:
            await host.aclose()

    async def test_a_subdirectory_still_watches(self, jail: Any) -> None:
        _outside, root = jail
        (root / "sub").mkdir()
        host = await self._host(root)
        try:
            client = await _client(host)
            response = await client.request(
                "createResourceWatch",
                {"channel": ROOT_URI, "uri": (root / "sub").as_uri()},
            )
            assert "error" not in response, response
        finally:
            await host.aclose()
