"""Approval mode through a real `Host`: a chat's config does not follow its session.

Security-relevant (AHP 1.1.0, upstream `c301d494`). The host seeds every chat's
`config` when it creates it and tells the agent the complete values; a later
`session/configChanged` must not loosen a chat that already exists, and a chat
created afterwards starts from the session's value at that moment.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host_claude.provider import ClaudeProvider, LocalClaudeSession
from tests.fakes import FakeClient

pytestmark = pytest.mark.anyio

MODE = "permissionMode"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Wire:
    """The least client that can drive `createSession`, `createChat` and dispatches."""

    def __init__(self, host: Host) -> None:
        self.host = host
        self._client, server = memory_pair()
        self._serving = asyncio.create_task(host.serve(server))
        self._ids = 0
        self._seq = 0
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        while True:
            message = await self._client.receive()
            if message is None:
                return
            identifier = message.get("id")
            waiter = (
                self._waiting.pop(identifier, None)
                if "method" not in message and isinstance(identifier, int)
                else None
            )
            if waiter is not None and not waiter.done():
                waiter.set_result(dict(message))

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._ids += 1
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[self._ids] = waiter
        await self._client.send(
            {"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params}
        )
        response = await asyncio.wait_for(waiter, timeout=5)
        assert "error" not in response, response
        return response

    async def dispatch(self, channel: str, action: dict[str, Any]) -> None:
        self._seq += 1
        await self._client.send(
            {
                "jsonrpc": "2.0",
                "method": "dispatchAction",
                "params": {"channel": channel, "clientSeq": self._seq, "action": action},
            }
        )

    async def until(self, ready: Any) -> None:
        for _ in range(500):
            if ready():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition never held")

    async def close(self) -> None:
        self._reader.cancel()
        self._serving.cancel()


def _state(host: Host, uri: str) -> dict[str, Any]:
    found = host.sequencer.state_of(uri)
    return dict(found) if isinstance(found, dict) else {}


async def test_loosening_the_session_does_not_loosen_existing_chats(tmp_path: Path) -> None:
    clients: list[FakeClient] = []

    def factory(options: Any) -> FakeClient:
        clients.append(FakeClient(options, []))
        return clients[-1]

    work = tmp_path / "work"
    work.mkdir()
    host = Host(ClaudeProvider(tmp_path, client_factory=factory), LoopbackSingleUserPolicy())
    wire = Wire(host)
    try:
        await wire.request(
            "initialize",
            {
                "channel": ROOT_URI,
                "clientId": "c1",
                "protocolVersions": ["1.0.0"],
                "initialSubscriptions": [ROOT_URI],
            },
        )
        uri = "claude:/cc-1"
        await wire.request(
            "createSession",
            {
                "channel": uri,
                "provider": "claude",
                "workingDirectories": [work.as_uri()],
                "config": {MODE: "default"},
            },
        )
        await wire.until(lambda: _state(host, uri).get("lifecycle") == "ready")
        await wire.request("subscribe", {"channel": uri})
        default = _state(host, uri)["defaultChat"]
        agent = host._sessions[uri].agent_session
        assert isinstance(agent, LocalClaudeSession)

        # Every chat, the default one included, has its own value from the start.
        assert _state(host, default)["config"]["values"][MODE] == "default"
        second = "ahp-chat:/cc-1b"
        await wire.request("createChat", {"channel": uri, "chat": second})
        assert _state(host, second)["config"]["values"][MODE] == "default"

        # Loosen the session.
        await wire.dispatch(uri, {"type": "session/configChanged", "config": {MODE: "auto"}})
        await wire.until(lambda: _state(host, uri)["config"]["values"][MODE] == "auto")
        await asyncio.sleep(0.05)

        assert agent.session_approvals == "auto"
        assert agent.approvals == "default", "the default chat was loosened"
        assert agent.chats[second].approvals == "default", "an existing chat was loosened"
        assert _state(host, default)["config"]["values"][MODE] == "default"
        assert _state(host, second)["config"]["values"][MODE] == "default"

        # A chat created now starts from the session's value.
        third = "ahp-chat:/cc-1c"
        await wire.request("createChat", {"channel": uri, "chat": third})
        assert _state(host, third)["config"]["values"][MODE] == "auto"
        assert agent.chats[third].approvals == "auto"
    finally:
        await wire.close()
        await host.aclose()
