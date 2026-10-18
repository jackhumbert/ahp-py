"""Client input on worker chats: steering only, and a provider can refuse it.

A worker chat (`SessionPublisher.open_tool_chat`) runs turns through
`ProviderChat.run_turn`. A client may steer the running turn, nothing else.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import AgentSessionContext, TurnSink, UserMessage
from ahp_host.provider.echo import EchoSession

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class _Knobs:
    interactivity: str = "full"
    gate: bool = False
    steered: list[str] = field(default_factory=list)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    worker: str = ""


class _WorkerSession(EchoSession):
    """Opens a worker chat during its turn and holds a turn on it."""

    def __init__(self, context: AgentSessionContext, knobs: _Knobs) -> None:
        super().__init__(context)
        self._knobs = knobs

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        publisher = self.context.publisher
        assert publisher is not None
        chat = await publisher.open_tool_chat(
            "Worker", tool_call_id="t1", interactivity=self._knobs.interactivity
        )
        self._knobs.worker = chat.resource

        async def run(inner: TurnSink) -> None:
            await self._knobs.release.wait()

        await chat.run_turn("work", run)
        self._knobs.opened.set()

    async def steer(self, chat_uri: str, message: UserMessage) -> bool:
        self._knobs.steered.append(message.text)
        return True

    def steering_rejection(self, chat_uri: str) -> str | None:
        return "no steering today" if self._knobs.gate else None


class _WorkerProvider(EchoProvider):
    def __init__(self, knobs: _Knobs) -> None:
        super().__init__()
        self._knobs = knobs

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        return _WorkerSession(context, self._knobs)


async def _start(interactivity: str) -> tuple[Host, FakeClient, _Knobs]:
    knobs = _Knobs(interactivity=interactivity)
    host = Host(_WorkerProvider(knobs), LoopbackSingleUserPolicy())
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "c1",
            "protocolVersions": ["1.0.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    uri = "echo:/ws-1"
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("chats")), timeout=10.0
    )
    chat_uri = host._sessions[uri].chat_uri
    await client.request("subscribe", {"channel": chat_uri})
    await client.notify(
        "dispatchAction",
        {
            "channel": chat_uri,
            "clientSeq": 1,
            "action": {
                "type": "chat/turnStarted",
                "turnId": "p1",
                "startedAt": "2026-01-01T00:00:00Z",
                "message": {"text": "go"},
            },
        },
    )
    await asyncio.wait_for(knobs.opened.wait(), 5)
    await client.request("subscribe", {"channel": knobs.worker})
    return host, client, knobs


async def _rejection(client: FakeClient, channel: str, seq: int, action: dict[str, Any]) -> Any:
    """Dispatch *action* and return the echoed envelope's `rejectionReason`."""
    await client.notify("dispatchAction", {"channel": channel, "clientSeq": seq, "action": action})

    def echoed() -> bool:
        return any((e.get("origin") or {}).get("clientSeq") == seq for e in client.actions(channel))

    await client.collect_until(echoed, timeout=5.0)
    (envelope,) = [
        e for e in client.actions(channel) if (e.get("origin") or {}).get("clientSeq") == seq
    ]
    return envelope.get("rejectionReason")


def _set(kind: str, text: str = "left") -> dict[str, Any]:
    return {
        "type": "chat/pendingMessageSet",
        "kind": kind,
        "id": "m1",
        "message": {"text": text},
    }


async def test_full_worker_chat_takes_steering() -> None:
    host, client, knobs = await _start("full")
    try:
        assert await _rejection(client, knobs.worker, 2, _set("steering")) is None
        await client.collect_until(
            lambda: (
                bool(knobs.steered)
                and not (host.sequencer.state_of(knobs.worker) or {}).get("queuedMessages")
            ),
            timeout=5.0,
        )
        assert knobs.steered == ["left"]
        assert not (host.sequencer.state_of(knobs.worker) or {}).get("queuedMessages")
    finally:
        knobs.release.set()
        await host.aclose()


async def test_read_only_worker_chat_refuses_steering() -> None:
    host, client, knobs = await _start("read-only")
    try:
        reason = await _rejection(client, knobs.worker, 2, _set("steering"))
        assert reason == "this worker chat is read-only"
        assert knobs.steered == []
    finally:
        knobs.release.set()
        await host.aclose()


async def test_provider_can_refuse_steering() -> None:
    host, client, knobs = await _start("full")
    try:
        knobs.gate = True
        reason = await _rejection(client, knobs.worker, 2, _set("steering"))
        assert reason == "no steering today"
        assert knobs.steered == []
    finally:
        knobs.release.set()
        await host.aclose()


async def test_client_cannot_start_a_turn_on_a_worker_chat() -> None:
    host, client, knobs = await _start("full")
    try:
        reason = await _rejection(
            client,
            knobs.worker,
            2,
            {
                "type": "chat/turnStarted",
                "turnId": "x",
                "startedAt": "2026-01-01T00:00:00Z",
                "message": {"text": "hi"},
            },
        )
        assert reason == "a worker chat's turns are started by its agent"
    finally:
        knobs.release.set()
        await host.aclose()


async def test_worker_chat_takes_no_queued_message() -> None:
    host, client, knobs = await _start("full")
    try:
        reason = await _rejection(client, knobs.worker, 2, _set("queued"))
        assert reason == "a worker chat takes only steering messages"
    finally:
        knobs.release.set()
        await host.aclose()


async def test_steering_after_the_worker_turn_ended_is_refused() -> None:
    host, client, knobs = await _start("full")
    try:
        knobs.release.set()

        def ended() -> bool:
            state = host.sequencer.state_of(knobs.worker) or {}
            return state.get("activeTurn") is None and bool(state.get("turns"))

        await client.collect_until(ended, timeout=5.0)
        reason = await _rejection(client, knobs.worker, 2, _set("steering"))
        assert reason == "no turn is running in this worker chat to steer"
    finally:
        await host.aclose()
