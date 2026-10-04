"""Worker chats a provider opens for a tool call (`SessionPublisher.open_tool_chat`).

The reverse edge of `ToolResultSubagentContent`: the chat's origin is
`{kind: "tool", chat, toolCallId}`. What makes a background subagent listable
as chat background work, which needs a chat of its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.errors import AhpError
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import SessionPublisher, TurnSink

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = Host(EchoProvider(), LoopbackSingleUserPolicy())
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
            "protocolVersions": ["1.0.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


async def _session(host: Host, client: FakeClient, uri: str) -> tuple[str, SessionPublisher]:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("chats")), timeout=10.0
    )
    publisher = host._sessions[uri].publisher
    assert publisher is not None
    return host._sessions[uri].chat_uri, publisher


async def test_a_tool_chat_is_catalogued_with_its_origin(host: Host) -> None:
    client = await _client(host)
    parent, publisher = await _session(host, client, "echo:/tc-1")
    chat = await publisher.open_tool_chat("Explore the repo", tool_call_id="call-7")
    state = host.sequencer.state_of(chat.resource) or {}
    assert state["origin"] == {"kind": "tool", "chat": parent, "toolCallId": "call-7"}
    assert state["interactivity"] == "read-only"
    assert state.get("movable") is not True, "it moves only with its parent"
    entry = next(
        c for c in host.sequencer.state_of("echo:/tc-1")["chats"] if c["resource"] == chat.resource
    )
    assert entry["origin"]["kind"] == "tool"


async def test_a_worker_turn_runs_and_ends(host: Host) -> None:
    client = await _client(host)
    _, publisher = await _session(host, client, "echo:/tc-2")
    chat = await publisher.open_tool_chat("Worker", tool_call_id="call-1")

    async def run(sink: TurnSink) -> None:
        await sink.text_delta("found 3 files")

    assert await chat.run_turn("look around", run)

    def ended() -> bool:
        state = host.sequencer.state_of(chat.resource) or {}
        return bool(state.get("turns")) and state.get("activeTurn") is None

    await client.collect_until(ended, timeout=5.0)
    (turn,) = (host.sequencer.state_of(chat.resource) or {})["turns"]
    assert turn["message"] == {"text": "look around", "origin": {"kind": "agent"}}


async def test_cancelling_a_worker_does_not_interrupt_the_agent(host: Host) -> None:
    client = await _client(host)
    _, publisher = await _session(host, client, "echo:/tc-3")
    chat = await publisher.open_tool_chat("Worker", tool_call_id="call-1")
    agent = host._sessions["echo:/tc-3"].agent_session
    assert agent is not None
    interrupted: list[str | None] = []

    async def cancel(reason: str | None = None) -> None:
        interrupted.append(reason)

    agent.cancel = cancel  # type: ignore[method-assign]
    stopped = asyncio.Event()

    async def run(sink: TurnSink) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.set()
            raise

    await chat.run_turn("work forever", run)
    await client.collect(seconds=0.1)
    active = (host.sequencer.state_of(chat.resource) or {})["activeTurn"]["id"]
    await client.request("subscribe", {"channel": chat.resource})
    await client.notify(
        "dispatchAction",
        {
            "channel": chat.resource,
            "clientSeq": 1,
            "action": {"type": "chat/turnCancelled", "turnId": active},
        },
    )
    await asyncio.wait_for(stopped.wait(), 5)
    assert interrupted == []


async def test_an_unknown_parent_is_refused(host: Host) -> None:
    client = await _client(host)
    _, publisher = await _session(host, client, "echo:/tc-4")
    with pytest.raises(AhpError):
        await publisher.open_tool_chat("x", tool_call_id="c", chat="ahp-chat:/nope")
