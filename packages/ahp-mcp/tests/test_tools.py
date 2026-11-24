"""The tools, end to end: MCP client -> ahp-mcp -> gateway -> stock hosts."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client as McpClient

from ahp_mcp import Fleet
from tests.support import World, mcp_client, text

TOOLS = [
    "cancel_session",
    "list_folder",
    "list_hosts",
    "list_sessions",
    "read_session",
    "send_prompt",
    "start_session",
    "wait_for_session",
]


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    built = World(tmp_path)
    yield built
    await built.aclose()


async def call(client: McpClient, tool: str, **arguments: Any) -> Any:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, text(result)
    body = text(result)
    try:
        return json.loads(body)
    except ValueError:
        return body


async def refused(client: McpClient, tool: str, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    assert result.is_error, text(result)
    return text(result)


async def test_the_tools_are_listed(world: World) -> None:
    async with mcp_client(Fleet(world.connect_gateway)) as mcp:
        listed = await mcp.list_tools()
        assert sorted(tool.name for tool in listed.tools) == TOOLS
        for tool in listed.tools:
            assert tool.description, tool.name


async def test_list_hosts_names_every_node_and_the_agents(world: World) -> None:
    async with mcp_client(Fleet(world.connect_gateway)) as mcp:
        listed = await call(mcp, "list_hosts")
        assert [h["host"] for h in listed["hosts"]] == ["node-a", "node-b"]
        assert listed["hosts"][0]["root"] == "ahp-file:///node-a"
        assert listed["agents"] == ["claude"]


async def test_list_folder_reads_a_node_relative_to_its_root(world: World) -> None:
    async with mcp_client(Fleet(world.connect_gateway)) as mcp:
        root = await call(mcp, "list_folder", host="node-a")
        assert sorted(e["name"] for e in root) == ["app", "notes.txt"]
        app = await call(mcp, "list_folder", host="node-a", folder="app")
        assert [e["name"] for e in app] == ["main.py"]
        again = await call(
            mcp, "list_folder", host="node-a", folder=app[0]["uri"].rsplit("/", 1)[0]
        )
        assert again == app


async def test_a_session_starts_on_the_named_host_in_the_named_folder(world: World) -> None:
    async with mcp_client(Fleet(world.connect_gateway)) as mcp:
        started = await call(
            mcp, "start_session", host="node-b", folder="game", prompt="hello there"
        )
        assert started["host"] == "node-b"
        assert started["folder"] == "ahp-file:///node-b/game"
        session = started["session"]

        waited = await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        assert waited["status"] == "done"
        assert waited["timed_out"] is False

        read = await call(mcp, "read_session", session=session)
        assert "> hello there" in read["transcript"]
        assert "hello there" in read["transcript"].split("\n", 1)[1]

        # It ran on node-b, in node-b's own folder, and node-a never heard of it.
        on_b = world.hosts["node-b"].sequencer.state_of(session)
        assert on_b is not None
        assert world.hosts["node-a"].sequencer.state_of(session) is None
        game = (world.roots["node-b"] / "game").resolve().as_uri()
        assert on_b["workingDirectories"] == [game]


async def test_an_absolute_folder_is_anywhere_on_the_host(world: World) -> None:
    async with mcp_client(Fleet(world.connect_gateway)) as mcp:
        folder = str(world.roots["node-a"] / "app")
        started = await call(mcp, "start_session", host="node-a", folder=folder, prompt="hi")
        assert started["folder"].startswith("ahp-file://node-a/")
        await call(mcp, "wait_for_session", session=started["session"], timeout_seconds=10)
        on_a = world.hosts["node-a"].sequencer.state_of(started["session"])
        assert on_a is not None
        assert on_a["workingDirectories"] == [Path(folder).resolve().as_uri()]


async def test_a_session_takes_another_prompt_and_is_listed(world: World) -> None:
    async with mcp_client(Fleet(world.connect_gateway)) as mcp:
        started = await call(mcp, "start_session", host="node-a", prompt="first", title="Two turns")
        session = started["session"]
        await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        assert "Sent" in await call(mcp, "send_prompt", session=session, prompt="second")
        await call(mcp, "wait_for_session", session=session, timeout_seconds=10)

        read = await call(mcp, "read_session", session=session, turns=5)
        assert "> first" in read["transcript"]
        assert "> second" in read["transcript"]

        listed = await call(mcp, "list_sessions")
        mine = [s for s in listed if s["session"] == session]
        assert mine
        assert mine[0]["host"] == "node-a"
        assert mine[0]["agent"] == "claude"


async def test_a_session_started_elsewhere_can_be_read(world: World, tmp_path: Path) -> None:
    async with mcp_client(Fleet(world.connect_gateway)) as first:
        started = await call(first, "start_session", host="node-a", prompt="from elsewhere")
        await call(first, "wait_for_session", session=started["session"], timeout_seconds=10)
    async with mcp_client(Fleet(world.connect_gateway)) as second:
        read = await call(second, "read_session", session=started["session"])
    assert "> from elsewhere" in read["transcript"]


async def test_refusals_are_tool_errors_the_model_can_read(world: World) -> None:
    async with mcp_client(Fleet(world.connect_gateway)) as mcp:
        assert "node-a, node-b" in await refused(mcp, "start_session", host="nowhere", prompt="x")
        assert "claude" in await refused(
            mcp, "start_session", host="node-a", prompt="x", agent="other"
        )
        assert "prompt is required" in await refused(
            mcp, "start_session", host="node-a", prompt=" "
        )
        assert "climbs" in await refused(mcp, "list_folder", host="node-a", folder="../b")


async def test_cancel_stops_a_running_turn(tmp_path: Path) -> None:
    world = World(tmp_path, delay=1.0)
    try:
        async with mcp_client(Fleet(world.connect_gateway)) as mcp:
            prompt = "a slow turn"
            started = await call(mcp, "start_session", host="node-a", prompt=prompt)
            session = started["session"]
            busy = await refused(mcp, "send_prompt", session=session, prompt="again")
            assert "busy" in busy
            assert "Cancelled" in await call(mcp, "cancel_session", session=session)
            waited = await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
            assert waited["status"] == "stopped"
            assert "not running" in await call(mcp, "cancel_session", session=session)
    finally:
        await world.aclose()


async def test_wait_times_out_on_a_long_turn(tmp_path: Path) -> None:
    world = World(tmp_path, delay=1.0)
    try:
        async with mcp_client(Fleet(world.connect_gateway)) as mcp:
            prompt = "a slow turn"
            started = await call(mcp, "start_session", host="node-a", prompt=prompt)
            waited = await call(
                mcp, "wait_for_session", session=started["session"], timeout_seconds=0.2
            )
            assert waited["status"] == "working"
            assert waited["timed_out"] is True
            assert waited["title"] == "a slow turn"
            await call(mcp, "cancel_session", session=started["session"])
    finally:
        await world.aclose()


async def test_a_lone_host_is_one_machine(world: World) -> None:
    async with mcp_client(Fleet(lambda: world.connect_host("node-b"))) as mcp:
        listed = await call(mcp, "list_hosts")
        assert [h["host"] for h in listed["hosts"]] == ["host"]
        assert [e["name"] for e in await call(mcp, "list_folder", host="host")] == ["game"]
        started = await call(mcp, "start_session", host="host", folder="game", prompt="solo")
        assert started["folder"] == (world.roots["node-b"] / "game").resolve().as_uri()
        waited = await call(mcp, "wait_for_session", session=started["session"])
        assert waited["status"] == "done"


async def test_session_config_comes_from_the_server_not_the_model(world: World) -> None:
    async with mcp_client(
        Fleet(world.connect_gateway, session_config={"permissionMode": "auto"})
    ) as mcp:
        listed = await mcp.list_tools()
        start = next(t for t in listed.tools if t.name == "start_session")
        assert "config" not in json.dumps(start.input_schema)
        assert "permission" not in json.dumps(start.input_schema).lower()


async def test_an_unreachable_endpoint_is_a_tool_error(world: World) -> None:
    def down() -> Any:
        async def dial() -> Any:
            raise OSError("connection refused")

        from ahp_client import connect

        return connect(transport_factory=dial, reconnect=False)

    async with mcp_client(Fleet(down, connect_timeout=2)) as mcp:
        assert "not reachable" in await refused(mcp, "list_hosts")
