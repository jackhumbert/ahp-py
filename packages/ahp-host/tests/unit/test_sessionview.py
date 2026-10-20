"""The publisher's session view, worker chat reopening and confirmation hints."""

from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import (
    AgentSessionContext,
    EchoProvider,
    EchoSession,
    ObservesChatState,
    ObservesSessionState,
    ReopensToolChats,
)
from ahp_host.provider.base import ToolConfirmation

from .hosting import connect, open_session, run_turn, shut, state

pytestmark = pytest.mark.anyio

_RESOURCE = {
    "resource": "https://mcp.example.invalid",
    "authorization_servers": ["https://auth.example.invalid"],
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Recording(EchoProvider):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[AgentSessionContext] = []

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        self.contexts.append(context)
        return await super().create_session(context)

    async def resume_session(self, context: AgentSessionContext) -> EchoSession:
        self.contexts.append(context)
        return await super().resume_session(context)


async def test_sessionview_publisher_is_observable() -> None:
    provider = Recording()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/sv-1"
        await open_session(wire, uri)
        publisher = provider.contexts[0].publisher
        assert publisher is not None
        assert isinstance(publisher, ObservesSessionState)
        assert isinstance(publisher, ReopensToolChats)

        await publisher.mcp_server_changed(
            "srv", {"kind": "authRequired", "reason": "required", "resource": _RESOURCE}
        )
        assert publisher.session_state()["inputNeeded"][0]["id"] == "mcp:srv"

        await publisher.mcp_server_changed("srv", {"kind": "ready"})
        assert "inputNeeded" not in publisher.session_state()
        assert publisher.session_state() == state(host, uri)
    finally:
        await shut(host, wire, serving)


async def test_sessionview_chat_state_shows_the_turns_message(tmp_path: Path) -> None:
    """`chat_state` reads a turn's message back, also after a restart from the store."""
    provider = Recording()
    store = FileSessionStore(tmp_path / "sessions", debounce=0.0)
    host = Host(provider, LoopbackSingleUserPolicy(), store=store)
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/sv-cs")
        publisher = provider.contexts[0].publisher
        assert isinstance(publisher, ObservesChatState)
        await run_turn(wire, chat, "t1", "remember this")
        (turn,) = publisher.chat_state()["turns"]
        assert (turn["id"], turn["message"]["text"]) == ("t1", "remember this")
        assert publisher.chat_state(chat) == state(host, chat)
        # A chat the session does not own reads as nothing.
        assert publisher.chat_state("chat:/someone-else") == {}
    finally:
        await shut(host, wire, serving)

    provider2 = Recording()
    host2 = Host(
        provider2, LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path / "sessions")
    )
    await host2.restore()
    wire2, serving2 = await connect(host2)
    try:
        await wire2.request("subscribe", {"channel": "echo:/sv-cs"})
        await run_turn(wire2, chat, "t2", "and this")
        publisher2 = provider2.contexts[0].publisher
        assert isinstance(publisher2, ObservesChatState)
        texts = [t["message"]["text"] for t in publisher2.chat_state(chat)["turns"]]
        assert texts == ["remember this", "and this"]
    finally:
        await shut(host2, wire2, serving2)


async def test_sessionview_worker_chat_reopens_only_tool_chats() -> None:
    provider = Recording()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/sv-2")
        publisher = provider.contexts[0].publisher
        assert isinstance(publisher, ReopensToolChats)
        chat = await publisher.open_tool_chat("w", tool_call_id="t1")

        again = publisher.worker_chat(chat.resource)
        assert again is not None
        assert again.resource == chat.resource

        async def run(sink: object) -> None:
            return None

        assert await again.run_turn("go", run)
        assert publisher.worker_chat(default) is None
        assert publisher.worker_chat("nope") is None
    finally:
        await shut(host, wire, serving)


def test_sessionview_confirmation_hints_default() -> None:
    confirmation = ToolConfirmation("c", "n", "m")
    assert confirmation.read_only is None
    assert confirmation.policy_asked is False


async def test_sessionview_worker_chat_reopens_after_a_restart(tmp_path: Path) -> None:
    provider = Recording()
    first = Host(provider, LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path))
    wire, serving = await connect(first)
    uri = "echo:/sv-3"
    try:
        default = await open_session(wire, uri)
        publisher = provider.contexts[0].publisher
        assert isinstance(publisher, ReopensToolChats)
        worker = (await publisher.open_tool_chat("w", tool_call_id="t1")).resource
        await run_turn(wire, default, "t1")
    finally:
        await shut(first, wire, serving)

    provider = Recording()
    second = Host(provider, LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path))
    assert await second.restore() == 1
    wire, serving = await connect(second)
    try:
        await wire.request("subscribe", {"channel": uri})
        await wire.request("subscribe", {"channel": default})
        await run_turn(wire, default, "t2")
        publisher = provider.contexts[-1].publisher
        assert isinstance(publisher, ReopensToolChats)
        again = publisher.worker_chat(worker)
        assert again is not None
        assert again.resource == worker

        async def run(sink: object) -> None:
            return None

        assert await again.run_turn("go", run)
        # Only the provider's chats come back as workers: never the default chat.
        assert publisher.worker_chat(default) is None
    finally:
        await shut(second, wire, serving)
