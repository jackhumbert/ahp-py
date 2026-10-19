"""Who sent a turn: the policy's `principal_of` reaching the provider.

The principal of the connection that created a session rides on
`AgentSessionContext.principal` (and in the store, so a restart keeps it); the
principal of the connection that dispatched a turn rides on
`UserMessage.principal`. A clientId is never an identity.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.policies import principal_from_header
from ahp_host.core.policy import PRINCIPAL_METADATA_KEY, ConnectionInfo
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import AgentSessionContext, TurnSink, UserMessage
from ahp_host.provider.echo import EchoSession
from tests.unit.hosting import Wire, finished, state, turn_started

pytestmark = pytest.mark.anyio

_HEADER = "x-forwarded-user"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class HeaderPolicy(LoopbackSingleUserPolicy):
    principal_of = staticmethod(principal_from_header(_HEADER))


def _raising(info: ConnectionInfo) -> str | None:
    raise RuntimeError("identity provider down")


class RaisingPolicy(LoopbackSingleUserPolicy):
    principal_of = staticmethod(_raising)


class RecordingSession(EchoSession):
    def __init__(self, context: AgentSessionContext, messages: list[UserMessage]) -> None:
        super().__init__(context)
        self.messages = messages

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self.messages.append(message)
        await super().send_user_message(message, sink)


class RecordingProvider(EchoProvider):
    """Keeps every context it was given and every message it was sent."""

    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[AgentSessionContext] = []
        self.messages: list[UserMessage] = []

    async def create_session(self, context: AgentSessionContext) -> RecordingSession:
        self.contexts.append(context)
        return RecordingSession(context, self.messages)

    async def resume_session(self, context: AgentSessionContext) -> RecordingSession:
        return await self.create_session(context)


async def _connect(host: Host, user: str | None, client_id: str) -> tuple[Wire, asyncio.Task[None]]:
    client, server = memory_pair()
    headers = {_HEADER: user} if user is not None else {}
    serving = asyncio.create_task(host.serve(server, peer="proxy", headers=headers))
    wire = Wire(host, client)
    response = await wire.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["1.0.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    assert "result" in response, response
    wire.initialized = dict(response["result"])
    return wire, serving


async def _create(wire: Wire, uri: str) -> str:
    """Create a session, subscribe, and return its default chat."""
    response = await wire.request("createSession", {"channel": uri, "provider": "echo"})
    assert "error" not in response, response
    assert await wire.until(lambda: state(wire.host, uri).get("lifecycle") == "ready")
    await wire.request("subscribe", {"channel": uri})
    chat = state(wire.host, uri)["defaultChat"]
    assert isinstance(chat, str)
    await wire.request("subscribe", {"channel": chat})
    return chat


async def _turn(wire: Wire, chat: str, turn_id: str) -> None:
    await wire.dispatch(chat, turn_started(turn_id))
    assert await wire.until(lambda: finished(wire.host, chat, turn_id)), state(wire.host, chat)


async def _close(host: Host, *wires: tuple[Wire, asyncio.Task[None]]) -> None:
    for wire, serving in wires:
        await wire.close()
        serving.cancel()
    await host.aclose()


async def test_hostprincipal_reaches_context_and_messages() -> None:
    provider = RecordingProvider()
    host = Host(provider, HeaderPolicy())
    alice = await _connect(host, "alice", "a1")
    bob = await _connect(host, "bob", "b1")
    try:
        chat = await _create(alice[0], "ahp-session:/s1")
        await bob[0].request("subscribe", {"channel": "ahp-session:/s1"})
        await bob[0].request("subscribe", {"channel": chat})
        await _turn(alice[0], chat, "t1")
        await _turn(bob[0], chat, "t2")
        assert [c.principal for c in provider.contexts] == ["alice"]
        assert [m.principal for m in provider.messages] == ["alice", "bob"]
    finally:
        await _close(host, alice, bob)


async def test_hostprincipal_none_without_the_hook() -> None:
    provider = RecordingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    alice = await _connect(host, "alice", "a1")
    try:
        chat = await _create(alice[0], "ahp-session:/s1")
        await _turn(alice[0], chat, "t1")
        assert [c.principal for c in provider.contexts] == [None]
        assert [m.principal for m in provider.messages] == [None]
    finally:
        await _close(host, alice)


async def test_hostprincipal_hook_failure_is_anonymous() -> None:
    provider = RecordingProvider()
    host = Host(provider, RaisingPolicy())
    alice = await _connect(host, "alice", "a1")
    try:
        chat = await _create(alice[0], "ahp-session:/s1")
        await _turn(alice[0], chat, "t1")
        assert [c.principal for c in provider.contexts] == [None]
        assert [m.principal for m in provider.messages] == [None]
    finally:
        await _close(host, alice)


async def test_hostprincipal_survives_a_store_restart(tmp_path: Path) -> None:
    uri = "ahp-session:/s1"

    def make(provider: RecordingProvider) -> Host:
        return Host(
            provider,
            HeaderPolicy(),
            store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
            sequence_file=tmp_path / "seq",
        )

    first_provider = RecordingProvider()
    first = make(first_provider)
    alice = await _connect(first, "alice", "a1")
    chat = await _create(alice[0], uri)
    await _turn(alice[0], chat, "t1")
    assert [m.principal for m in first_provider.messages] == ["alice"]
    await _close(first, alice)

    records = await FileSessionStore(tmp_path / "sessions").load_all()
    assert [dict(r.metadata or {}) for r in records] == [{PRINCIPAL_METADATA_KEY: "alice"}]

    provider = RecordingProvider()
    host = make(provider)
    assert await host.restore() == 1
    bob = await _connect(host, "bob", "b1")
    try:
        await bob[0].request("subscribe", {"channel": uri})
        chat = state(host, uri)["defaultChat"]
        await bob[0].request("subscribe", {"channel": chat})
        await _turn(bob[0], chat, "t2")
        assert [c.principal for c in provider.contexts] == ["alice"]
        assert [m.principal for m in provider.messages] == ["bob"]
    finally:
        await _close(host, bob)
