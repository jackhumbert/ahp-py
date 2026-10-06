"""One agent session, many chats: which chat a turn is for, and which exist.

`send_user_message` carried no chat, so every chat's turn ran in one agent
conversation -- a side chat's question was answered with the default chat's
history in context, and its answer leaked into that history. These pin the
three things a provider needs to run a conversation per chat: the chat on
every message (`UserMessage.chat_uri`), the chat lifecycle (`HostsChats`),
and a cancel that names the chat (`CancelsChats`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol import errors

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import (
    AgentSessionContext,
    CancelsChats,
    ChatContext,
    HostsChats,
    TurnSink,
    UserMessage,
)

from .hosting import Wire, connect, finished, open_session, run_turn, shut, state, turn_started

pytestmark = pytest.mark.anyio

_CHATS: dict[str, Any] = {
    "multipleChats": {"fork": True, "sideChat": True},
    "multipleWorkingDirectories": {},
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Hosting(EchoSession):
    def __init__(self, context: AgentSessionContext, delay: float = 0.0) -> None:
        super().__init__(context, delay=delay)
        self.messages: list[UserMessage] = []
        self.opened: list[ChatContext] = []
        self.closed: list[str] = []
        self.cancelled: list[tuple[str, str | None]] = []
        self.session_cancels = 0
        self.refuse: Exception | None = None

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self.messages.append(message)
        await super().send_user_message(message, sink)

    async def chat_opened(self, context: ChatContext) -> None:
        if self.refuse is not None:
            raise self.refuse
        self.opened.append(context)

    async def chat_closed(self, chat_uri: str) -> None:
        self.closed.append(chat_uri)

    async def cancel_chat(self, chat_uri: str, reason: str | None = None) -> None:
        self.cancelled.append((chat_uri, reason))

    async def cancel(self, reason: str | None = None) -> None:
        self.session_cancels += 1
        await super().cancel(reason)


class HostingProvider(EchoProvider):
    def __init__(self, *, delay: float = 0.0, gate: asyncio.Event | None = None) -> None:
        super().__init__(capabilities=_CHATS)
        self.sessions: list[Hosting] = []
        self._session_delay = delay
        self._gate = gate

    async def create_session(self, context: AgentSessionContext) -> Hosting:
        if self._gate is not None:
            await self._gate.wait()
        session = Hosting(context, delay=self._session_delay)
        self.sessions.append(session)
        return session

    async def resume_session(self, context: AgentSessionContext) -> Hosting:
        session = Hosting(context, delay=self._session_delay)
        self.sessions.append(session)
        return session


async def _chat(wire: Wire, session: str, chat: str, **params: Any) -> dict[str, Any]:
    response = await wire.request("createChat", {"channel": session, "chat": chat, **params})
    if "error" not in response:
        await wire.request("subscribe", {"channel": chat})
    return response


def test_the_protocols_are_feature_detected_and_echo_has_neither() -> None:
    hosting = Hosting.__new__(Hosting)
    assert isinstance(hosting, HostsChats)
    assert isinstance(hosting, CancelsChats)
    echo = EchoSession.__new__(EchoSession)
    assert not isinstance(echo, HostsChats)
    assert not isinstance(echo, CancelsChats)


async def test_every_message_names_its_chat() -> None:
    provider = HostingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/hc-1"
        default = await open_session(wire, uri)
        await _chat(wire, uri, "ahp-chat:/hc-1-side")
        await run_turn(wire, default, "t1", "to the default")
        await run_turn(wire, "ahp-chat:/hc-1-side", "t2", "to the second")
        agent = provider.sessions[0]
        assert [(m.chat_uri, m.text) for m in agent.messages] == [
            (default, "to the default"),
            ("ahp-chat:/hc-1-side", "to the second"),
        ]
    finally:
        await shut(host, wire, serving)


async def test_a_new_chat_is_announced_with_its_origin() -> None:
    provider = HostingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/hc-2"
        directory = "file:///work/a"
        default = await open_session(wire, uri, workingDirectories=[directory])
        await run_turn(wire, default, "t1", "first")
        await run_turn(wire, default, "t2", "second")
        agent = provider.sessions[0]

        await _chat(wire, uri, "ahp-chat:/plain", workingDirectories=[directory])
        await _chat(
            wire, uri, "ahp-chat:/fork", source={"kind": "fork", "chat": default, "turnId": "t1"}
        )
        await _chat(
            wire,
            uri,
            "ahp-chat:/side",
            source={"kind": "sideChat", "chat": default, "turnId": "t2"},
        )
        plain, fork, side = agent.opened
        assert plain.chat_uri == "ahp-chat:/plain"
        assert plain.origin is None
        assert plain.fork is None
        assert plain.side_chat is None
        assert plain.working_directories == (directory,)
        assert not plain.restored

        # A fork's copied turns are its history; the agent gets them.
        assert fork.origin_kind == "fork"
        assert fork.fork is not None
        assert (fork.fork.chat_uri, fork.fork.turn_id) == (default, "t1")
        assert [t["id"] for t in fork.fork.turns] == ["t1"]
        assert fork.working_directories is None, "a fork inherits; it has no subset"

        # A side chat's are context only -- its own history is empty.
        assert side.origin_kind == "sideChat"
        assert side.side_chat is not None
        assert side.fork is None
        assert [t["id"] for t in side.side_chat.turns] == ["t1", "t2"]
        assert state(host, "ahp-chat:/side")["turns"] == []
        # The default chat is `AgentSessionContext.chat_uri`, never announced.
        assert default not in [c.chat_uri for c in agent.opened]
    finally:
        await shut(host, wire, serving)


@pytest.mark.parametrize(
    ("refusal", "code"),
    [(errors.AhpError(-32009, "not here"), -32009), (RuntimeError("no"), -32603)],
)
async def test_a_provider_that_refuses_fails_create_chat_with_nothing_created(
    refusal: Exception, code: int
) -> None:
    provider = HostingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/hc-3"
        await open_session(wire, uri)
        provider.sessions[0].refuse = refusal
        response = await _chat(wire, uri, "ahp-chat:/refused")
        assert response["error"]["code"] == code
        assert not host.sequencer.has_channel("ahp-chat:/refused")
        assert "ahp-chat:/refused" not in [c["resource"] for c in state(host, uri)["chats"]]
        assert not [
            a
            for a in wire.actions(uri, "session/chatAdded")
            if a["summary"]["resource"] == "ahp-chat:/refused"
        ]
    finally:
        await shut(host, wire, serving)


async def test_dispose_chat_closes_it_after_its_turn_is_cancelled() -> None:
    provider = HostingProvider(delay=0.5)
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/hc-4"
        await open_session(wire, uri)
        chat = "ahp-chat:/hc-4-side"
        await _chat(wire, uri, chat)
        await wire.dispatch(chat, turn_started("t1"))
        assert await wire.until(lambda: state(host, chat).get("activeTurn") is not None)

        response = await wire.request("disposeChat", {"channel": chat})
        assert "error" not in response, response
        agent = provider.sessions[0]
        assert agent.closed == [chat]
        # The chat's own cancel, not the session's.
        assert agent.cancelled == [(chat, "chat disposed")]
        assert agent.session_cancels == 0
    finally:
        await shut(host, wire, serving)


async def test_cancelling_one_chat_names_it_while_another_runs() -> None:
    """Without `CancelsChats` the host withholds `cancel()` while any other
    chat is running; with it, the agent hears exactly which chat to stop."""
    provider = HostingProvider(delay=0.4)
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/hc-5"
        default = await open_session(wire, uri)
        side = "ahp-chat:/hc-5-side"
        await _chat(wire, uri, side)
        await wire.dispatch(default, turn_started("t1"))
        await wire.dispatch(side, turn_started("t2"))
        assert await wire.until(lambda: state(host, side).get("activeTurn") is not None)

        await wire.dispatch(side, {"type": "chat/turnCancelled", "turnId": "t2", "duration": 1})
        agent = provider.sessions[0]
        assert await wire.until(lambda: bool(agent.cancelled))
        assert agent.cancelled == [(side, "client cancelled")]
        assert agent.session_cancels == 0
        assert await wire.until(lambda: finished(host, default, "t1"))
    finally:
        await shut(host, wire, serving)


async def test_a_chat_created_during_bring_up_is_announced_when_the_agent_arrives() -> None:
    gate = asyncio.Event()
    provider = HostingProvider(gate=gate)
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/hc-6"
        assert "error" not in await wire.request(
            "createSession", {"channel": uri, "provider": "echo"}
        )
        await wire.until(lambda: host.sequencer.has_channel(uri))
        assert "error" not in await wire.request(
            "createChat", {"channel": uri, "chat": "ahp-chat:/early"}
        )
        gate.set()
        assert await wire.until(lambda: bool(provider.sessions and provider.sessions[0].opened))
        [early] = provider.sessions[0].opened
        assert early.chat_uri == "ahp-chat:/early"
        assert early.restored
    finally:
        await shut(host, wire, serving)


async def test_a_restored_session_replays_its_chats_to_the_resumed_agent(
    tmp_path: Path,
) -> None:
    first = Host(HostingProvider(), LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path))
    wire, serving = await connect(first)
    uri = "echo:/hc-7"
    try:
        default = await open_session(wire, uri)
        await _chat(wire, uri, "ahp-chat:/kept")
        await run_turn(wire, default, "t1")
    finally:
        await shut(first, wire, serving)

    provider = HostingProvider()
    second = Host(provider, LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path))
    assert await second.restore() == 1
    wire, serving = await connect(second)
    try:
        await wire.request("subscribe", {"channel": uri})
        await wire.request("subscribe", {"channel": default})
        assert provider.sessions == [], "resumed lazily, on the first turn"
        await run_turn(wire, default, "t2")
        [agent] = provider.sessions
        assert [(c.chat_uri, c.restored) for c in agent.opened] == [("ahp-chat:/kept", True)]
        assert agent.messages[-1].chat_uri == default
    finally:
        await shut(second, wire, serving)


async def test_a_moved_chat_leaves_one_agent_and_joins_the_other() -> None:
    provider = HostingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/hc-8a")
        await open_session(wire, "echo:/hc-8b")
        source, target = provider.sessions
        chat = "ahp-chat:/travelling"
        await _chat(wire, "echo:/hc-8a", chat)
        response = await wire.request(
            "moveChat",
            {"channel": chat, "destination": {"kind": "session", "session": "echo:/hc-8b"}},
        )
        assert "error" not in response, response
        assert source.closed == [chat]
        [arrived] = target.opened
        assert arrived.chat_uri == chat
        assert arrived.moved_from == "echo:/hc-8a"
        assert arrived.session_uri == "echo:/hc-8b"
    finally:
        await shut(host, wire, serving)


async def test_a_worker_chat_is_not_announced_but_is_closed() -> None:
    """You opened it, so you hold it; a client disposing it is still news."""
    provider = HostingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/hc-9")
        agent = provider.sessions[0]
        assert agent.context.publisher is not None
        worker = await agent.context.publisher.open_tool_chat("Worker", tool_call_id="call-1")
        assert agent.opened == []
        response = await wire.request("disposeChat", {"channel": worker.resource})
        assert "error" not in response, response
        assert agent.closed == [worker.resource]
    finally:
        await shut(host, wire, serving)


async def test_a_forked_session_names_the_chat_and_turn_it_came_from() -> None:
    """`ForkedFrom` grew `chat_uri` and `turn_id`, so a session fork and a chat
    fork describe their source the same way."""
    provider = HostingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/hc-10")
        await run_turn(wire, default, "t1")
        await run_turn(wire, default, "t2")
        await open_session(
            wire, "echo:/hc-10-fork", fork={"session": "echo:/hc-10", "turnId": "t1"}
        )
        fork = provider.sessions[-1].context.fork
        assert fork is not None
        assert (fork.session_uri, fork.chat_uri, fork.turn_id) == ("echo:/hc-10", default, "t1")
        assert [t["id"] for t in fork.turns] == ["t1"]
    finally:
        await shut(host, wire, serving)
