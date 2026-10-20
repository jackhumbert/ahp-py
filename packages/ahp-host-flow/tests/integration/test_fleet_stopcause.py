"""A fleet call the run itself stops ends its node session; only the host stopping detaches.

The Scheduler marks a live stop (budget, wall time, quota) on the call's `StopCause` before it
cancels the call, and a `call_timeout` scope is on it too, so `FleetDispatcher` sends
`stop_session` for those; a cancellation with no abort and no stop cause is the host stopping,
and the node session is left running for the resumed run to reattach to.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tests.fleet_fakes import FakeFleet, join_fake_gateway
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import found, notifications, run_turn, state

FLEET = """
[providers.alpha]
via = "fleet"
node = "alpha"
agent = "claude"
"""
ROUTING = """
[routing]
default = "alpha"
"""
HOLD = (
    "meta = {'name': 'hold', 'description': 'd'}\n\n"
    "async def main(args):\n    return await agent('hold on')\n"
)


async def boot(tmp_path: Path, toml: str) -> FlowFixture:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "hold.py").write_text(HOLD, encoding="utf-8")
    return await boot_flow(tmp_path, "node", toml, provider_options={"fleet_client_wait": 5.0})


async def _run_hold(fx: FlowFixture, fleet: FakeFleet) -> dict[str, Any]:
    client = await join_fake_gateway(fx.host, SESSION, fleet)
    try:
        assert await fx.wire.until(lambda: bool(state(fx.host, SESSION).get("activeClients")))
        return await asyncio.wait_for(run_turn(fx.wire, fx.chat, "t1", "/hold"), 10)
    finally:
        await client.aclose()


async def test_fleet_stopcause_wall_budget_stops_the_node_session(tmp_path: Path) -> None:
    fx = await boot(tmp_path, FLEET + ROUTING + "[limits]\nwall_budget = 0.3\n")
    fleet = FakeFleet()
    try:
        turn = await _run_hold(fx, fleet)
    finally:
        await fx.shut()
    (uri,) = fleet.sessions
    # The live stop is the run's own: the node session must not keep spending.
    assert fleet.stopped == [uri]
    assert fleet.sessions[uri]["status"] == "stopped"
    # agent() throws in the script, so the run fails naming the budget.
    assert "wall budget exhausted" in json.dumps(turn)
    # The main chat's agent tool call is completed, not left running.
    assert not found(turn, lambda m: m.get("status") == "running")


async def test_fleet_stopcause_call_timeout_stops_the_node_session(tmp_path: Path) -> None:
    fx = await boot(tmp_path, FLEET + "call_timeout = 0.3\n" + ROUTING)
    fleet = FakeFleet()
    try:
        turn = await _run_hold(fx, fleet)
    finally:
        await fx.shut()
    assert final_json(turn) is None
    (uri,) = fleet.sessions
    assert fleet.stopped == [uri]
    assert any("took longer than 0.3s (call_timeout)" in note for note in notifications(turn))
    (done,) = found(turn, lambda m: m.get("pastTenseMessage") == "agent() failed")
    assert done["success"] is False
