"""A chat whose config the agent could not be told is not run (fail closed).

`ReconfiguresChats.chat_config_changed` carries what the chat may do (for one
agent, its approval mode). An agent that never heard a chat's values falls back
to the session's, which may be looser than the chat's own -- so a failed
delivery must not be logged and forgotten: `createChat` fails with nothing
created, and a chat that existed already has its turns refused until a later
delivery succeeds.
"""

from __future__ import annotations

from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import AgentSessionContext, EchoProvider, EchoSession

from .hosting import connect, open_session, run_turn, shut, state

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_CHATS: dict[str, Any] = {"multipleChats": {"fork": True, "sideChat": True}}


class _FlakySession(EchoSession):
    """Refuses to be told a chat's values while the provider says so."""

    def __init__(self, context: AgentSessionContext, provider: _FlakyProvider) -> None:
        super().__init__(context)
        self._flaky = provider

    async def chat_config_changed(self, chat: str, values: Any) -> None:
        if self._flaky.failing:
            raise RuntimeError("cannot apply chat config")
        await super().chat_config_changed(chat, values)


class _FlakyProvider(EchoProvider):
    def __init__(self) -> None:
        super().__init__(configurable=True, capabilities=_CHATS)
        self.failing = False

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        return _FlakySession(context, self)


async def _chat(wire: Any, session: str, chat: str) -> dict[str, Any]:
    response = await wire.request("createChat", {"channel": session, "chat": chat})
    assert isinstance(response, dict)
    return response


async def test_create_chat_fails_with_nothing_created_when_the_agent_cannot_be_told() -> None:
    provider = _FlakyProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/fc-1")
        provider.failing = True
        response = await _chat(wire, "echo:/fc-1", "ahp-chat:/fc-1b")
        assert "error" in response, response
        assert not host.sequencer.has_channel("ahp-chat:/fc-1b")
        session = host._sessions["echo:/fc-1"]
        assert "ahp-chat:/fc-1b" not in session.chat_uris
        assert "ahp-chat:/fc-1b" not in session.opened_chats
        assert "ahp-chat:/fc-1b" not in session.published_chats
        assert all(
            c.get("resource") != "ahp-chat:/fc-1b" for c in state(host, "echo:/fc-1")["chats"]
        )
        # And the URI is free again.
        provider.failing = False
        assert "error" not in await _chat(wire, "echo:/fc-1", "ahp-chat:/fc-1b")
    finally:
        await shut(host, wire, serving)


async def _failed_turn(host: Host, chat: str, turn: str) -> dict[str, Any] | None:
    return next((t for t in state(host, chat).get("turns", []) if t["id"] == turn), None)


async def test_a_chat_whose_values_could_not_be_delivered_at_attach_refuses_turns() -> None:
    provider = _FlakyProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/fc-2")
        session = host._sessions["echo:/fc-2"]
        provider.failing = True
        await host._attach_agent(session, _FlakySession(session.agent_session.context, provider))  # type: ignore[union-attr]
        assert default in session.undelivered_chats
        await run_turn(wire, default, "t1")
        turn = await _failed_turn(host, default, "t1")
        assert turn is not None
        assert turn["state"] == "error"
        [part] = turn["responseParts"]
        # Nothing reached the agent: the only part is the refusal.
        assert part["kind"] == "error"
        assert "config" in part["error"]["message"]
    finally:
        await shut(host, wire, serving)


async def test_a_later_successful_delivery_lifts_the_refusal() -> None:
    provider = _FlakyProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/fc-3")
        session = host._sessions["echo:/fc-3"]
        provider.failing = True
        await host._attach_agent(session, _FlakySession(session.agent_session.context, provider))  # type: ignore[union-attr]
        assert default in session.undelivered_chats
        provider.failing = False
        # A client changing the chat's values delivers them again.
        seq = await wire.dispatch(
            default, {"type": "chat/configChanged", "config": {"prefix": "Hi:"}}
        )
        await wire.echoed(default, seq)
        assert default not in session.undelivered_chats
        await run_turn(wire, default, "t2")
        turn = await _failed_turn(host, default, "t2")
        assert turn is not None
        assert turn["state"] == "complete"
    finally:
        await shut(host, wire, serving)


async def test_the_next_turn_retries_the_delivery() -> None:
    provider = _FlakyProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/fc-4")
        session = host._sessions["echo:/fc-4"]
        provider.failing = True
        await host._attach_agent(session, _FlakySession(session.agent_session.context, provider))  # type: ignore[union-attr]
        provider.failing = False
        await run_turn(wire, default, "t3")
        turn = await _failed_turn(host, default, "t3")
        assert turn is not None
        assert turn["state"] == "complete"
        assert default not in session.undelivered_chats
    finally:
        await shut(host, wire, serving)
