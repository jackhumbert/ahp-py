"""Waiting on, reading and bounding the sessions a flow run started.

All in memory: the nodes are stock hosts over memory transports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_client import TurnCompleted, TurnFailed

from ahp_gateway.core.orchestrator import _Spawned
from tests.fleet import Fleet
from tests.flowfleet import flow_fleet, run_tool, start_flow

pytestmark = pytest.mark.anyio

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _text(result: Mapping[str, Any]) -> str:
    return "\n".join(
        part["text"] for part in result.get("content") or [] if part.get("type") == "text"
    )


async def _close(fleet: Fleet) -> None:
    await fleet.gateway.aclose()
    await fleet.aclose()


async def _start(fleet: Fleet, uri: str = "flow:/r1", node: str = "alpha", **extra: Any) -> Any:
    return await run_tool(
        fleet, uri, "start_session", node=node, agent="claude", **{"prompt": "hi", **extra}
    )


async def test_fleetgw_wait_structured_result_text_and_usage(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        first = (await _start(fleet, prompt='{"n": 3}', output_schema=SCHEMA))["structuredContent"]
        waited = await run_tool(fleet, "flow:/r1", "wait_for_sessions", sessions=[first["session"]])
        body = waited["structuredContent"]
        assert body["timed_out"] is False
        assert isinstance(body["sessions"][0].pop("activity"), int)
        assert body["sessions"] == [{"session": first["session"], "status": "done", "done": True}]

        read = (await run_tool(fleet, "flow:/r1", "read_session", session=first["session"]))[
            "structuredContent"
        ]
        assert read["structured"] == {"n": 3}
        assert read["usage"]["output_tokens"] >= 1
        assert read["status"] == "done"

        second = (await _start(fleet, prompt="hello"))["structuredContent"]
        await run_tool(fleet, "flow:/r1", "wait_for_sessions", sessions=[second["session"]])
        plain = (await run_tool(fleet, "flow:/r1", "read_session", session=second["session"]))[
            "structuredContent"
        ]
        assert "hello" in plain["text"]
        assert plain["structured"] is None
    finally:
        await _close(fleet)


async def test_fleetgw_wait_a_session_the_node_never_had_is_gone(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        orchestrator = fleet.gateway.orchestrator
        assert orchestrator is not None
        orchestrator._adopted["flow:/r1"].children["claude:/missing"] = _Spawned(
            uri="claude:/missing", node="alpha", parent="flow:/r1", title="x"
        )
        waited = await run_tool(
            fleet, "flow:/r1", "wait_for_sessions", sessions=["claude:/missing"]
        )
        assert waited["structuredContent"]["sessions"] == [
            {"session": "claude:/missing", "status": "gone", "done": True}
        ]
        read = await run_tool(fleet, "flow:/r1", "read_session", session="claude:/missing")
        assert read["success"] is False
        assert "is gone" in _text(read)
    finally:
        await _close(fleet)


async def test_fleetgw_wait_max_running_waits_for_a_slot(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path, max_running=1)
    try:
        await start_flow(fleet)
        first = (await _start(fleet, node="asker", prompt="wait"))["structuredContent"]
        alpha = fleet.hosts["alpha"].sequencer._states
        task = asyncio.create_task(_start(fleet, prompt="second"))
        await asyncio.sleep(0.3)
        assert not task.done()
        # Waiting for the slot, not started: alpha has no session yet.
        assert not [u for u, s in alpha.items() if isinstance(s, Mapping) and "defaultChat" in s]
        await run_tool(fleet, "flow:/r1", "stop_session", session=first["session"])
        result = await asyncio.wait_for(task, 5)
        assert result["success"] is True
    finally:
        await _close(fleet)


async def test_fleetgw_wait_slot_wait_gives_up(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path, max_running=1, slot_wait=0.2)
    try:
        await start_flow(fleet)
        await _start(fleet, node="asker", prompt="wait")
        refused = await _start(fleet, prompt="second")
        assert refused["success"] is False
        assert "still running" in _text(refused)
    finally:
        await _close(fleet)


async def test_fleetgw_wait_another_run_cannot_read_or_wait(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet, "flow:/r1", "surface-1")
        await start_flow(fleet, "flow:/r2", "surface-2")
        started = (await _start(fleet))["structuredContent"]
        for tool, arguments in (
            ("read_session", {"session": started["session"]}),
            ("wait_for_sessions", {"sessions": [started["session"]]}),
        ):
            result = await run_tool(fleet, "flow:/r2", tool, **arguments)
            assert result["success"] is False
            assert "not a session you started" in _text(result)
    finally:
        await _close(fleet)


async def test_fleetgw_wait_another_user_cannot_prompt_the_flow_session(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet, "flow:/r1", "surface-1")
        async with fleet.surface("surface-2") as other:
            session = await other.open_session("flow:/r1")
            chat = await session.chat()
            refused = await chat.prompt("hi")
        assert isinstance(refused, TurnFailed)
        assert "another user" in refused.reason

        async with fleet.surface("surface-1") as owner:
            session = await owner.open_session("flow:/r1")
            chat = await session.chat()
            assert isinstance(await chat.prompt("hi"), TurnCompleted)
    finally:
        await _close(fleet)


async def test_fleetgw_wait_activity_changes_with_the_session(tmp_path: Path) -> None:
    """`activity` lets an engine see progress between status changes (its idle watchdog)."""
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        held = (await _start(fleet, node="asker", prompt="wait"))["structuredContent"]

        async def row() -> Any:
            waited = await run_tool(
                fleet, "flow:/r1", "wait_for_sessions", sessions=[held["session"]], timeout=0.1
            )
            return waited["structuredContent"]["sessions"][0]

        before = await row()
        assert before["activity"] == (await row())["activity"]
        await run_tool(fleet, "flow:/r1", "stop_session", session=held["session"])
        after = await row()
        assert after["done"] is True
        assert after["activity"] != before["activity"]
    finally:
        await fleet.gateway.aclose()
        await fleet.aclose()
