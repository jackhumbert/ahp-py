"""relay_command and relay_host: the relay reached over a named host, and the config keys."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import UserMessage

from ahp_host_acp.agent import provider_from_options
from ahp_host_acp.client_tools import RelayTool, ToolBridge
from ahp_host_acp.config import ConfigError
from ahp_host_acp.provider import AcpProvider, AgentSpec
from ahp_host_acp.relay import TOKEN_ENV
from ahp_host_acp.roots import Roots

from .fakes import FAKE_AGENT, RecordingSink, context

_PONG = {"content": [{"type": "text", "text": "pong"}], "isError": False}
_TOOL = RelayTool(
    name="ping",
    published="ping",
    title="Ping",
    description="Reply with pong",
    input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    client_id="gateway",
)
PING = {
    "name": "ping",
    "description": "Reply pong",
    "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}},
}
RELAY = (sys.executable, "-m", "ahp_host_acp.relay")


class Relay:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process

    async def ask(self, message: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()
        return await self.read()

    async def read(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        line = await asyncio.wait_for(self.process.stdout.readline(), 10)
        assert line, "the relay closed its output"
        return json.loads(line)  # type: ignore[no-any-return]


async def test_acprelayhost_bridge_round_trip() -> None:
    async def call(key: str, tool: RelayTool, arguments: dict[str, Any]) -> dict[str, Any]:
        return _PONG

    bridge = ToolBridge(lambda key: [_TOOL], call, host="localhost", relay_command=RELAY)
    await bridge.start()
    process: asyncio.subprocess.Process | None = None
    try:
        server = bridge.server("chat-1")
        assert server.command == (*RELAY, "--port", str(bridge.port), "--host", "localhost")
        assert server.to_acp({})["command"] == sys.executable  # type: ignore[index]
        assert not any(bridge.token_for("chat-1") in part for part in server.command)
        process = await asyncio.create_subprocess_exec(
            *server.command,
            env={**os.environ, **server.env},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        relay = Relay(process)
        init = await relay.ask({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert "result" in init
        called = await relay.ask(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {"n": 1}},
            }
        )
        assert called["result"] == _PONG
    finally:
        await bridge.aclose()
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def test_acprelayhost_session_entry(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    spec = AgentSpec(
        FAKE_AGENT,
        env={"FAKE_ACP_LOG": str(log)},
        relay_host="localhost",
        relay_command=RELAY,
    )
    session = await AcpProvider(tmp_path, spec).create_session(
        context(tmp_path, active_client_id="c1", client_tools=(PING,))
    )
    sink = RecordingSink()
    try:
        await session.send_user_message(UserMessage(text='mcp call ping {"n": 1}'), sink)
    finally:
        await session.aclose()
    said = "".join(str(e[1]) for e in sink.events if e[0] == "text")
    assert json.loads(said)["chose"] == "once"
    assert "pong" in said
    opened = [
        entry["params"]
        for entry in map(json.loads, log.read_text().splitlines())
        if entry["method"] == "session/new"
    ]
    (params,) = opened
    relay = params["mcpServers"][-1]
    assert relay["command"] == sys.executable
    assert relay["args"][-2:] == ["--host", "localhost"]
    token = next(e["value"] for e in relay["env"] if e["name"] == TOKEN_ENV)
    assert token not in relay["args"]


def test_acprelayhost_config_keys(tmp_path: Path) -> None:
    def spec(**extra: Any) -> AgentSpec:
        provider = provider_from_options(
            {"command": ["agent"], **extra}, Roots.single(tmp_path), tmp_path
        )
        return provider.spec

    chosen = spec(relay_command=["python3", "-m", "ahp_host_acp.relay"], relay_host="172.17.0.1")
    assert chosen.relay_command == ("python3", "-m", "ahp_host_acp.relay")
    assert chosen.relay_host == "172.17.0.1"
    assert spec().relay_command is None
    assert spec().relay_host is None
    for bad in ("python3", [], [""], [1]):
        with pytest.raises(ConfigError, match="relay_command must be a list of non-empty strings"):
            spec(relay_command=bad)
    for bad_host in ("", 5, ["h"]):
        with pytest.raises(ConfigError, match="relay_host must be a non-empty string"):
            spec(relay_host=bad_host)
