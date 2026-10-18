"""`ClientDispatcher` against a second in-memory host, without a worker chat (plan §8 M7)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from ahp_client import connect
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.provider.echo import EchoProvider
from ahp_protocol.transport import Transport, memory_pair

from ahp_host_flow.engine import (
    AgentCall,
    ClientDispatcher,
    DispatchHandle,
    RemoteAgentError,
    RemoteSpec,
)
from ahp_host_flow.engine.dispatcher import _REATTACH
from tests.fleet_hosting import GatedEchoProvider
from tests.hosting import all_channels, state


class Far:
    """A second host, dialed through a memory transport."""

    def __init__(self, provider: EchoProvider | None = None) -> None:
        self.provider = provider or EchoProvider(provider_id="claude", structured=True)
        self.host = Host(self.provider, LoopbackSingleUserPolicy())
        self.serving: set[asyncio.Task[None]] = set()

    async def dial(self) -> Transport:
        client_end, server_end = memory_pair()
        task = asyncio.create_task(self.host.serve(server_end))
        self.serving.add(task)
        task.add_done_callback(self.serving.discard)
        return client_end

    def titles(self) -> list[object]:
        sessions = [u for u in all_channels(self.host) if "defaultChat" in state(self.host, u)]
        return [state(self.host, u).get("title") for u in sessions]


@pytest.fixture
async def far() -> AsyncIterator[Far]:
    remote = Far()
    yield remote
    for task in list(remote.serving):
        task.cancel()


async def test_remote_dispatch_engine_returns_the_remote_reply(far: Far) -> None:
    dispatcher = ClientDispatcher(RemoteSpec("far", "claude", transport_factory=far.dial))
    try:
        result = await dispatcher.run_agent(AgentCall(0, "hi"))
        assert result.value == "You said: hi"
        assert result.usage["output_tokens"] >= 1
    finally:
        await dispatcher.aclose()


async def test_remote_dispatch_engine_structured_result(far: Far) -> None:
    dispatcher = ClientDispatcher(RemoteSpec("far", "claude", transport_factory=far.dial))
    try:
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        result = await dispatcher.run_agent(AgentCall(0, '{"n": 1}', {"schema": schema}))
        assert result.value == {"n": 1}
    finally:
        await dispatcher.aclose()


async def test_remote_dispatch_engine_titles_the_remote_session(far: Far) -> None:
    dispatcher = ClientDispatcher(RemoteSpec("far", "claude", transport_factory=far.dial))
    try:
        await dispatcher.run_agent(AgentCall(0, "hi", label="Check"))
        deadline = asyncio.get_running_loop().time() + 5
        while "Check" not in far.titles() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert "Check" in far.titles()
    finally:
        await dispatcher.aclose()


async def _eventually(ready: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not ready():
            await asyncio.sleep(0.01)


def _last_state(far: Far, session: str) -> object:
    chat = state(far.host, str(state(far.host, session)["defaultChat"]))
    if chat.get("activeTurn") is not None:
        return "running"
    turns = chat.get("turns") or []
    return turns[-1].get("state") if turns else None


async def test_remote_dispatch_engine_a_question_nobody_answers_fails_the_call() -> None:
    """A bare client cannot answer the remote agent's question: bounded, not a hang."""
    far = Far(EchoProvider(provider_id="claude", elicit=True))
    dispatcher = ClientDispatcher(
        RemoteSpec("far", "claude", transport_factory=far.dial, idle_timeout=0.3)
    )
    try:
        with pytest.raises(RemoteAgentError, match=r"waited 0\.3s with nothing happening"):
            await asyncio.wait_for(dispatcher.run_agent(AgentCall(0, "hi")), 10)
        (session,) = [u for u in all_channels(far.host) if "defaultChat" in state(far.host, u)]
        await _eventually(lambda: _last_state(far, session) == "cancelled")
    finally:
        await dispatcher.aclose()
        for task in list(far.serving):
            task.cancel()


async def test_remote_dispatch_engine_a_cut_off_call_stops_its_old_remote_turn() -> None:
    """Not reattached: the turn a restart left on the remote host is stopped, the call rerun."""
    provider = GatedEchoProvider(provider_id="claude")
    provider.gate.clear()
    far = Far(provider)
    lines: list[str] = []
    dispatcher = ClientDispatcher(
        RemoteSpec("far", "claude", transport_factory=far.dial), on_log=lines.append
    )
    async with connect(transport_factory=far.dial, client_id="old-run") as old:
        session = await old.create_session(provider="claude")
        chat = await session.chat()

        async def hold() -> object:
            return await chat.prompt("hi", idle_timeout=None)

        held = asyncio.create_task(hold())
        await _eventually(lambda: _last_state(far, session.uri) == "running")
        token = _REATTACH.set(DispatchHandle("far", None, session.uri))
        try:
            call = asyncio.create_task(dispatcher.run_agent(AgentCall(3, "hi")))
        finally:
            _REATTACH.reset(token)
        try:
            await _eventually(lambda: _last_state(far, session.uri) == "cancelled")
            provider.gate.set()
            result = await asyncio.wait_for(call, 10)
            assert result.value == "You said: hi"
            assert lines == [
                f"agent() #3: stopped remote session {session.uri} that the restart cut off; "
                "dispatching it again"
            ]
        finally:
            provider.gate.set()
            held.cancel()
            await dispatcher.aclose()
            for task in list(far.serving):
                task.cancel()
