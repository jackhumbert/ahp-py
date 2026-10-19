"""`origin_turn`: which client turn a worker chat works for, and when it is nobody's.

A worker chat's own turns are host-internal, so a policy that binds something
to each client turn needs the way back. The host records it when the worker is
opened and fails closed: no active turn, a stale turn, or a worker whose own
origin is unknown all give ``None``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import (
    AgentSessionContext,
    OriginTurn,
    ResolvesOriginTurn,
    SessionPublisher,
    TurnSink,
    UserMessage,
)

from .hosting import Wire, connect, open_session, run_turn, shut, state, turn_started

pytestmark = pytest.mark.anyio

_CHATS: dict[str, Any] = {"multipleChats": {"fork": True, "sideChat": True}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Calling(EchoSession):
    """`call:<id>` starts that tool call and holds the turn open; anything else ends at once."""

    def __init__(self, context: AgentSessionContext) -> None:
        super().__init__(context)
        self.started: set[str] = set()

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        if message.text.startswith("call:"):
            call_id = message.text.removeprefix("call:")
            await sink.tool_call_started(call_id, "agent", {})
            self.started.add(call_id)
            await asyncio.Event().wait()
        await super().send_user_message(message, sink)

    async def cancel(self, reason: str | None = None) -> None:
        pass


class CallingProvider(EchoProvider):
    def __init__(self) -> None:
        super().__init__(capabilities=_CHATS)
        self.sessions: list[Calling] = []

    async def create_session(self, context: AgentSessionContext) -> Calling:
        session = Calling(context)
        self.sessions.append(session)
        return session


class Rig:
    def __init__(self, host: Host, wire: Wire, serving: asyncio.Task[None]) -> None:
        self.host, self.wire, self.serving = host, wire, serving
        self.provider = CallingProvider()

    async def session(self, uri: str) -> tuple[str, Calling, ResolvesOriginTurn]:
        chat = await open_session(self.wire, uri)
        agent = self.provider.sessions[-1]
        publisher = agent.context.publisher
        assert isinstance(publisher, ResolvesOriginTurn)
        return chat, agent, publisher

    async def call(self, chat: str, agent: Calling, turn: str, call_id: str) -> None:
        await self.wire.dispatch(chat, turn_started(turn, f"call:{call_id}"))
        assert await self.wire.until(lambda: call_id in agent.started)
        # Started in this turn, not an earlier one.
        assert state(self.host, chat)["activeTurn"]["id"] == turn


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    provider = CallingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    made = Rig(host, wire, serving)
    made.provider = provider
    try:
        yield made
    finally:
        await shut(host, wire, serving)


async def _worker(publisher: SessionPublisher, call_id: str, chat: str | None = None) -> str:
    opened = await publisher.open_tool_chat("w", tool_call_id=call_id, chat=chat)
    return opened.resource


async def test_a_worker_resolves_the_turn_that_made_its_call_and_nested_workers_inherit(
    rig: Rig,
) -> None:
    chat, agent, publisher = await rig.session("echo:/ot-1")
    await rig.call(chat, agent, "t1", "c1")
    worker = await _worker(agent.context.publisher, "c1")  # type: ignore[arg-type]
    nested = await _worker(agent.context.publisher, "c2", chat=worker)  # type: ignore[arg-type]
    expected = OriginTurn(chat, "t1")
    assert publisher.origin_turn(worker) == expected
    assert publisher.origin_turn(nested) == expected
    assert rig.host.origin_turn(nested) == expected
    # Not a worker chat: nothing to resolve.
    assert publisher.origin_turn() is None
    assert rig.host.origin_turn(chat) is None


async def test_a_worker_opened_with_no_active_turn_resolves_nothing(rig: Rig) -> None:
    chat, agent, publisher = await rig.session("echo:/ot-2")
    await run_turn(rig.wire, chat, "t1")
    worker = await _worker(agent.context.publisher, "c1")  # type: ignore[arg-type]
    assert publisher.origin_turn(worker) is None
    assert rig.host.origin_turn(worker) is None


async def test_a_worker_under_an_unbound_worker_resolves_nothing(rig: Rig) -> None:
    chat, agent, publisher = await rig.session("echo:/ot-3")
    await run_turn(rig.wire, chat, "t1")
    orphan = await _worker(agent.context.publisher, "c1")  # type: ignore[arg-type]
    # The orphan has no active turn of its own to fall back to, and must not use one.
    await rig.host._run_external_turn(rig.host._sessions["echo:/ot-3"], orphan, "x", _hold, "agent")
    child = await _worker(agent.context.publisher, "c2", chat=orphan)  # type: ignore[arg-type]
    assert publisher.origin_turn(child) is None


async def _hold(sink: TurnSink) -> None:
    await asyncio.Event().wait()


async def test_a_late_worker_is_not_bound_to_a_later_turn(rig: Rig) -> None:
    chat, agent, publisher = await rig.session("echo:/ot-4")
    await rig.call(chat, agent, "t1", "c1")
    await rig.wire.dispatch(chat, {"type": "chat/turnCancelled", "turnId": "t1", "duration": 1})
    await rig.call(chat, agent, "t2", "c2")
    stale = await _worker(agent.context.publisher, "c1")  # type: ignore[arg-type]
    fresh = await _worker(agent.context.publisher, "c2")  # type: ignore[arg-type]
    assert publisher.origin_turn(stale) is None
    assert publisher.origin_turn(fresh) == OriginTurn(chat, "t2")


async def test_two_chats_in_one_session_resolve_their_own_turns(rig: Rig) -> None:
    chat, agent, publisher = await rig.session("echo:/ot-5")
    other = "ahp-chat:/ot-5-b"
    response = await rig.wire.request("createChat", {"channel": "echo:/ot-5", "chat": other})
    assert "error" not in response, response
    await rig.wire.request("subscribe", {"channel": other})
    await rig.call(chat, agent, "ta", "ca")
    await rig.call(other, agent, "tb", "cb")
    a = await _worker(agent.context.publisher, "ca", chat=chat)  # type: ignore[arg-type]
    b = await _worker(agent.context.publisher, "cb", chat=other)  # type: ignore[arg-type]
    assert publisher.origin_turn(a) == OriginTurn(chat, "ta")
    assert publisher.origin_turn(b) == OriginTurn(other, "tb")


async def test_removing_or_moving_a_worker_chat_clears_its_origin(rig: Rig) -> None:
    chat, agent, publisher = await rig.session("echo:/ot-6")
    await rig.call(chat, agent, "t1", "c1")
    gone = await _worker(agent.context.publisher, "c1")  # type: ignore[arg-type]
    moved = await _worker(agent.context.publisher, "c1")  # type: ignore[arg-type]
    assert publisher.origin_turn(gone) is not None
    response = await rig.wire.request("disposeChat", {"channel": gone})
    assert "error" not in response, response
    assert rig.host.origin_turn(gone) is None
    assert publisher.origin_turn(moved) is not None
    session = rig.host._sessions["echo:/ot-6"]
    rig.host._hand_over(session, None, [moved])
    assert rig.host.origin_turn(moved) is None


async def test_a_publisher_answers_only_for_its_own_session(rig: Rig) -> None:
    chat, agent, publisher = await rig.session("echo:/ot-7")
    await rig.call(chat, agent, "t1", "c1")
    worker = await _worker(agent.context.publisher, "c1")  # type: ignore[arg-type]
    _, _, other = await rig.session("echo:/ot-8")
    assert other.origin_turn(worker) is None
    assert publisher.origin_turn(worker) == OriginTurn(chat, "t1")
