"""The stdio MCP relay and the loopback bridge behind it, with a real subprocess."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ahp_host.provider.base import ToolResult

from ahp_host_acp.client_tools import (
    RelayTool,
    ToolBridge,
    is_relayed_title,
    mcp_result,
    merged,
    offered,
)
from ahp_host_acp.relay import TOKEN_ENV

_PONG = {"content": [{"type": "text", "text": "pong"}], "isError": False}
_TOOL = RelayTool(
    name="ping",
    published="ping",
    title="Ping",
    description="Reply with pong",
    input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    client_id="gateway",
)


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


class Harness:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.bridge = ToolBridge(lambda key: [_TOOL], self._call)

    async def _call(self, key: str, tool: RelayTool, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((key, tool.name, arguments))
        return _PONG

    async def spawn(self, **env: str) -> asyncio.subprocess.Process:
        server = self.bridge.server("chat-1")
        return await asyncio.create_subprocess_exec(
            *server.command,
            env={**os.environ, **server.env, **env},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    harness = Harness()
    await harness.bridge.start()
    try:
        yield harness
    finally:
        await harness.bridge.aclose()


@pytest.fixture
async def relay(harness: Harness) -> AsyncIterator[Relay]:
    process = await harness.spawn()
    try:
        yield Relay(process)
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 10)


def _request(request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


async def test_tool_relay_initializes(relay: Relay) -> None:
    answer = await relay.ask(_request(1, "initialize", {"protocolVersion": "2025-06-18"}))
    assert answer["id"] == 1
    assert answer["result"]["protocolVersion"] == "2025-06-18"
    assert answer["result"]["capabilities"]["tools"]["listChanged"] is True


async def test_tool_relay_lists_the_tools(relay: Relay) -> None:
    answer = await relay.ask(_request(2, "tools/list"))
    assert answer["result"]["tools"] == [_TOOL.to_mcp()]


async def test_tool_relay_runs_a_call_through_the_host(relay: Relay, harness: Harness) -> None:
    answer = await relay.ask(_request(3, "tools/call", {"name": "ping", "arguments": {"n": 1}}))
    assert answer["result"] == _PONG
    assert harness.calls == [("chat-1", "ping", {"n": 1})]


async def test_tool_relay_announces_changed_tools(relay: Relay, harness: Harness) -> None:
    # The notification only reaches a relay that has authenticated, which the
    # first answer proves.
    await relay.ask(_request(1, "ping"))
    harness.bridge.tools_changed("chat-1")
    assert await relay.read() == {
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
    }


async def test_tool_relay_answers_ping(relay: Relay) -> None:
    assert (await relay.ask(_request(4, "ping")))["result"] == {}


async def test_tool_relay_refuses_an_unknown_method(relay: Relay) -> None:
    answer = await relay.ask(_request(5, "resources/list"))
    assert answer["error"]["code"] == -32601


async def test_tool_relay_reports_an_unknown_tool(relay: Relay, harness: Harness) -> None:
    answer = await relay.ask(_request(6, "tools/call", {"name": "nope", "arguments": {}}))
    assert answer["result"]["isError"] is True
    assert "nope" in answer["result"]["content"][0]["text"]
    assert harness.calls == []


async def test_tool_relay_exits_cleanly_when_stdin_closes(relay: Relay) -> None:
    assert relay.process.stdin is not None
    relay.process.stdin.close()
    assert await asyncio.wait_for(relay.process.wait(), 10) == 0


async def test_tool_relay_is_refused_a_wrong_token(harness: Harness) -> None:
    process = await harness.spawn(**{TOKEN_ENV: "not-the-token"})
    _, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 1
    assert b"refused" in stderr
    assert harness.calls == []


def test_tool_relay_tokens_are_per_key() -> None:
    bridge = ToolBridge(lambda key: [], _unused)
    one, two = bridge.token_for("a"), bridge.token_for("b")
    assert one != two
    assert bridge.token_for("a") == one
    assert len(one) >= 32


async def test_tool_relay_revoke_forgets_the_token_and_closes_the_relay(
    harness: Harness,
) -> None:
    process = await harness.spawn()
    relay = Relay(process)
    try:
        await relay.ask(_request(1, "ping"))  # authenticated
        old = harness.bridge.token_for("chat-1")
        harness.bridge.revoke("chat-1")
        # The relay it had is closed, so it exits ...
        await asyncio.wait_for(process.wait(), 10)
        # ... and the old token no longer opens a new one.
        refused = await harness.spawn(**{TOKEN_ENV: old})
        _, stderr = await asyncio.wait_for(refused.communicate(), 10)
        assert refused.returncode == 1
        assert b"refused" in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 10)
    assert harness.bridge.token_for("chat-1") != old


async def _unused(key: str, tool: RelayTool, arguments: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("not called")


def test_tool_relay_offers_each_published_tool_once() -> None:
    tools = offered(
        [
            {"clientId": "a", "tools": [{"name": "x.y"}, {"name": "shared"}]},
            {"clientId": "b", "tools": [{"name": "shared"}, {"name": "own"}]},
            {"clientId": "c"},  # no tools at all
        ]
    )
    assert [(t.name, t.published, t.client_id) for t in tools] == [
        ("x_y", "x.y", "a"),
        ("shared", "shared", "a"),
        ("own", "own", "b"),
    ]
    assert tools[0].input_schema == {"type": "object", "properties": {}}


def test_tool_relay_merged_drops_a_colliding_client_tool() -> None:
    host = RelayTool("StructuredOutput", "StructuredOutput", "t", "d", {}, "ahp-py/host")
    clash = RelayTool("StructuredOutput", "StructuredOutput", "t", "d", {}, "gateway")
    other = RelayTool("other", "other", "t", "d", {}, "gateway")
    assert merged([host], [clash, other]) == (host, other)


def test_tool_relay_titles() -> None:
    names = {"ping"}
    assert is_relayed_title("ahp_client_ping", names)
    assert is_relayed_title("mcp__ahp_client__ping", names)
    assert not is_relayed_title("exec: ahp_client_ping", names)
    assert not is_relayed_title("ahp_client_other", names)
    assert not is_relayed_title("ping", names)


def test_tool_relay_result_of_a_run_tool() -> None:
    result = mcp_result(ToolResult(value={"content": [{"type": "text", "text": "ran"}]}))
    assert result == {"content": [{"type": "text", "text": "ran"}], "isError": False}
    assert mcp_result(ToolResult(value={}))["content"][0]["text"] == "Done."


def test_tool_relay_result_of_a_declined_tool() -> None:
    result = mcp_result(ToolResult(response="decline", reason="no such node"))
    assert result == {"content": [{"type": "text", "text": "no such node"}], "isError": True}


async def test_tool_relay_speaks_utf8_whatever_the_locale(harness: Harness) -> None:
    # Without UTF-8 mode the relay's stdio would default to the locale code page
    # (cp1252 on Windows) and "\r\n"; MCP stdio is UTF-8 with "\n". U+0081 is a
    # byte cp1252 cannot decode at all.
    process = await harness.spawn(PYTHONUTF8="0", PYTHONIOENCODING="")
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        text = "caf\u00e9 \u00fc\u00df \u2603 \U0001f600 \u0081"
        call = _request(4, "tools/call", {"name": "ping", "arguments": {"s": text}})
        process.stdin.write((json.dumps(call, ensure_ascii=False) + "\n").encode("utf-8"))
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line.endswith(b"\n")
        assert not line.endswith(b"\r\n")
        assert json.loads(line)["result"] == _PONG
        assert harness.calls == [("chat-1", "ping", {"s": text})]
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 10)


async def test_tool_relay_bridge_closes_with_a_connection_in_its_handshake() -> None:
    # A connection that never sends its token is closed by `aclose` too, so the
    # server's wait for its connections cannot hold teardown up (Windows proactor).
    bridge = ToolBridge(lambda key: [_TOOL], _unused)
    await bridge.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
    await asyncio.sleep(0.05)
    try:
        await asyncio.wait_for(bridge.aclose(), 3)
        assert await asyncio.wait_for(reader.read(), 3) == b""
    finally:
        writer.close()
