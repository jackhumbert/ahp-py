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

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.channels import ROOT_URI
from agent_host_server.provider import EchoProvider
from agent_host_server.provider.base import AgentSessionContext, TruncatesHistory
from agent_host_server.provider.echo import EchoSession
from agent_host_server.transport import memory_pair
from agent_host_server.types.protocol import SessionStatus

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
        await client.collect(seconds=0.5)

        assert _state(host, uri)["title"] == "add a retry to the fetch helper"
        assert _catalogue(host, uri, chat)["title"] == "New Chat"


class TestTheCatalogueKeepsUp:
    async def test_a_finished_turn_moves_the_entry(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/c-3"
        chat = await _session(host, client, uri)
        before = dict(_catalogue(host, uri, chat))

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.6)

        after = _catalogue(host, uri, chat)
        assert after["modifiedAt"] > before["modifiedAt"], "the entry never moved"
        assert after["modifiedAt"] == _state(host, chat)["modifiedAt"], "catalogue drifted"

    async def test_a_working_chat_says_so_in_the_catalogue(self, slow: Host) -> None:
        client = await _client(slow)
        uri = "echo:/c-4"
        chat = await _session(slow, client, uri)

        await _turn(client, chat, text="hello there this takes a while")
        await asyncio.sleep(0.25)
        during = _catalogue(slow, uri, chat)["status"]

        await client.collect(seconds=1.2)
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
        await client.collect(seconds=0.6)

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
        await client.collect(seconds=0.6)

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
        await client.collect(seconds=0.6)

        entry = _catalogue(host, uri, "ahp-chat:/aside")
        assert entry["modifiedAt"] == _state(host, "ahp-chat:/aside")["modifiedAt"]


class TestTruncation:
    """The most dangerous of the parity gaps: the user is not underserved, they
    are misinformed."""

    async def _truncate(self, client: FakeClient, chat: str, seq: int, **extra: Any) -> None:
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": seq,
                "action": {"type": "chat/truncated", **extra},
            },
        )
        await client.collect(seconds=0.5)

    async def test_the_provider_is_told(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/x-1"
        chat = await _session(host, client, uri)
        await _turn(client, chat, text="hello", turn="t1")
        await client.collect(seconds=0.5)

        await self._truncate(client, chat, 2, turnId="t1")

        session = host._sessions[uri].agent_session
        assert isinstance(session, TruncatesHistory | EchoSession)
        assert session.truncated == [(chat, "t1")]

    async def test_an_absent_turn_id_means_everything(self, host: Host) -> None:
        client = await _client(host)
        uri = "echo:/x-2"
        chat = await _session(host, client, uri)
        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.5)

        await self._truncate(client, chat, 2)

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
        await client.collect(seconds=0.5)

        await self._truncate(client, chat, 2, turnId=None)

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
        await asyncio.sleep(0.25)
        assert _state(slow, chat)["activeTurn"] is not None

        await self._truncate(client, chat, 2)
        await client.collect(seconds=0.8)

        assert _state(slow, chat)["activeTurn"] is None
        assert slow._sessions[uri].turn is not None
        assert slow._sessions[uri].turn.done()  # type: ignore[union-attr]

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
            await client.collect(seconds=0.5)
            client.notifications.clear()

            await self._truncate(client, chat, 2, turnId="t1")

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
