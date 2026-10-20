"""The gateway's per-principal `max_running_per_principal` over a user's flow sessions (M16).

All in memory: the nodes are stock hosts over memory transports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.fleet import Fleet
from tests.flowfleet import flow_fleet, run_tool, start_flow

pytestmark = pytest.mark.anyio


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


async def _start(fleet: Fleet, uri: str, node: str = "alpha", **extra: Any) -> Any:
    return await run_tool(
        fleet, uri, "start_session", node=node, agent="claude", **{"prompt": "hi", **extra}
    )


async def test_quota_gateway_principal_cap(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path, max_running=4, max_running_per_principal=1, slot_wait=0.2)
    try:
        await start_flow(fleet, "flow:/r1", "surface-1")
        await start_flow(fleet, "flow:/r2", "surface-1")
        await start_flow(fleet, "flow:/r3", "surface-2")
        first = await _start(fleet, "flow:/r1", node="asker", prompt="wait")
        assert first["success"] is True
        # The same principal's other flow session is held back by the first's child.
        refused = await _start(fleet, "flow:/r2")
        assert refused["success"] is False
        assert "1 session of yours is still running across your runs" in _text(refused)
        # Another principal is not.
        other = await _start(fleet, "flow:/r3")
        assert other["success"] is True
    finally:
        await _close(fleet)


async def test_quota_gateway_cap_waits(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path, max_running=4, max_running_per_principal=1, slot_wait=5)
    try:
        await start_flow(fleet, "flow:/r1", "surface-1")
        await start_flow(fleet, "flow:/r2", "surface-1")
        first = (await _start(fleet, "flow:/r1", node="asker", prompt="wait"))["structuredContent"]
        alpha = fleet.hosts["alpha"].sequencer._states
        task = asyncio.create_task(_start(fleet, "flow:/r2", prompt="second"))
        await asyncio.sleep(0.3)
        assert not task.done()
        # Waiting for the slot, not started: alpha has no session yet.
        assert not [u for u, s in alpha.items() if isinstance(s, Mapping) and "defaultChat" in s]
        await run_tool(fleet, "flow:/r1", "stop_session", session=first["session"])
        result = await asyncio.wait_for(task, 5)
        assert result["success"] is True
    finally:
        await _close(fleet)


async def test_quota_gateway_waiting_flow_does_not_hold_back_the_principal(tmp_path: Path) -> None:
    # Only r1's own `max_running` is full: its second start waits for r1's slot, and r2 (the
    # same principal, under the per-principal cap) starts meanwhile instead of queueing behind.
    fleet = flow_fleet(tmp_path, max_running=1, max_running_per_principal=3, slot_wait=5)
    try:
        await start_flow(fleet, "flow:/r1", "surface-1")
        await start_flow(fleet, "flow:/r2", "surface-1")
        first = (await _start(fleet, "flow:/r1", node="asker", prompt="wait"))["structuredContent"]
        blocked = asyncio.create_task(_start(fleet, "flow:/r1", prompt="second"))
        await asyncio.sleep(0.2)
        assert not blocked.done()
        other = await asyncio.wait_for(_start(fleet, "flow:/r2"), 2)
        assert other["success"] is True
        assert not blocked.done()
        await run_tool(fleet, "flow:/r1", "stop_session", session=first["session"])
        assert (await asyncio.wait_for(blocked, 5))["success"] is True
        # Nothing is left counting or reserved once the starts are over.
        orchestrator = fleet.gateway.orchestrator
        assert orchestrator is not None
        assert orchestrator._principal_gates == {}
    finally:
        await _close(fleet)


async def test_quota_gateway_one_session_wording(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path, max_running=1, slot_wait=0.2)
    try:
        await start_flow(fleet, "flow:/r1", "surface-1")
        assert (await _start(fleet, "flow:/r1", node="asker", prompt="wait"))["success"] is True
        refused = await _start(fleet, "flow:/r1")
        assert "1 of your sessions is still running after waiting" in _text(refused)
    finally:
        await _close(fleet)
