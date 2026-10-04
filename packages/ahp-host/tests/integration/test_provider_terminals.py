"""Terminals a provider opens, and their retention (1.0.0).

"A terminal URI referenced by `ToolResultTerminalContent.resource` remains the
single subscription identity after its command completes ... When its
lifecycle is `exited`, subscriptions MUST return an exited `TerminalState`
with the retained `TerminalContentPart[]`. The server MAY reconstruct this
state lazily from persisted output." Here it is persisted with the session and
restored exited.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.errors import AhpError
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import SessionPublisher

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _host(tmp_path: Path | None = None) -> Host:
    if tmp_path is None:
        return Host(EchoProvider(capabilities={"multipleChats": {}}), LoopbackSingleUserPolicy())
    return Host(
        EchoProvider(capabilities={"multipleChats": {}}),
        LoopbackSingleUserPolicy(),
        store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
        sequence_file=tmp_path / "seq",
    )


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = _host()
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
    chat: str = host._sessions[uri].chat_uri
    publisher = host._sessions[uri].publisher
    assert publisher is not None
    return chat, publisher


async def _snapshot(client: FakeClient, channel: str) -> dict[str, Any] | None:
    response = await client.request("subscribe", {"channel": channel})
    snapshot = response.get("result", {}).get("snapshot")
    return None if snapshot is None else dict(snapshot["state"])


def _text(state: dict[str, Any]) -> str:
    return "".join(part.get("value", "") for part in state["content"])


class TestLive:
    async def test_output_and_exit_reach_a_subscriber(self, host: Host) -> None:
        client = await _client(host)
        chat, publisher = await _session(host, client, "echo:/pt-1")
        terminal = await publisher.open_terminal(
            "npm test", turn_id="t1", tool_call_id="call-1", cwd="file:///work"
        )
        state = await _snapshot(client, terminal.resource)
        assert state is not None
        assert state["claim"] == {
            "kind": "session",
            "session": "echo:/pt-1",
            "chat": chat,
            "turnId": "t1",
            "toolCallId": "call-1",
        }
        assert state["isPty"] is False
        assert state["lifecycle"] == {"status": "running"}

        await terminal.write("ok 1\n")
        await terminal.write("ok 2\n")
        await terminal.exited(0)
        await terminal.write("ignored after exit")
        state = await _snapshot(client, terminal.resource)
        assert state is not None
        assert state["lifecycle"] == {"status": "exited", "exitCode": 0}
        assert _text(state) == "ok 1\nok 2\n"

    async def test_it_is_not_in_the_root_terminal_catalogue(self, host: Host) -> None:
        client = await _client(host)
        _, publisher = await _session(host, client, "echo:/pt-2")
        terminal = await publisher.open_terminal("build")
        root = host.sequencer.state_of(ROOT_URI) or {}
        assert terminal.resource not in [t.get("resource") for t in root.get("terminals") or []]

    async def test_input_is_refused_because_the_session_holds_it(self, host: Host) -> None:
        client = await _client(host)
        _, publisher = await _session(host, client, "echo:/pt-3")
        terminal = await publisher.open_terminal("build")
        await client.request("subscribe", {"channel": terminal.resource})
        await client.notify(
            "dispatchAction",
            {
                "channel": terminal.resource,
                "clientSeq": 1,
                "action": {"type": "terminal/input", "data": "rm -rf /\n"},
            },
        )
        await client.collect(seconds=0.2)
        echoes = [
            a for a in client.actions(terminal.resource) if a["action"]["type"] == "terminal/input"
        ]
        assert echoes, "no echo came back to say whether it was taken"
        assert all(e.get("rejectionReason") for e in echoes)

    async def test_an_unknown_chat_is_refused(self, host: Host) -> None:
        client = await _client(host)
        _, publisher = await _session(host, client, "echo:/pt-4")
        with pytest.raises(AhpError):
            await publisher.open_terminal("x", chat="ahp-chat:/nope")

    async def test_it_goes_with_its_chat(self, host: Host) -> None:
        client = await _client(host)
        _, publisher = await _session(host, client, "echo:/pt-5")
        side = "ahp-chat:/pt-5-side"
        await client.request("createChat", {"channel": "echo:/pt-5", "chat": side})
        terminal = await publisher.open_terminal("x", chat=side)
        await client.request("disposeChat", {"channel": side})
        assert host.sequencer.state_of(terminal.resource) is None


async def test_an_exited_terminal_is_retained_across_a_restart(tmp_path: Path) -> None:
    first = _host(tmp_path)
    try:
        client = await _client(first)
        _, publisher = await _session(first, client, "echo:/pt-restart")
        finished = await publisher.open_terminal("finished")
        await finished.write("all green\n")
        await finished.exited(0)
        running = await publisher.open_terminal("still going")
        await running.write("half")
        await first._persist(first._sessions["echo:/pt-restart"])
    finally:
        await first.aclose()

    second = _host(tmp_path)
    try:
        assert await second.restore() == 1
        client = await _client(second)
        state = await _snapshot(client, finished.resource)
        assert state is not None
        assert state["lifecycle"] == {"status": "exited", "exitCode": 0}
        assert _text(state) == "all green\n"
        # Its process died with the previous host, so it comes back exited too.
        state = await _snapshot(client, running.resource)
        assert state is not None
        assert state["lifecycle"] == {"status": "exited"}
        assert _text(state) == "half"
    finally:
        await second.aclose()
