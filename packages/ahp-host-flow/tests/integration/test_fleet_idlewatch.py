"""`idle_timeout` on a fleet provider: the node session's progress, not only its status.

A fleet call sees no deltas. Its watchdog is reset by a status change or by a change in the
row's `activity` (the gateway's fingerprint of the node session's current turn), sampled at
least twice per `idle_timeout`; a `waiting` row pauses it. On expiry the node session is
stopped and the call fails with kind `timeout`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ahp_host_flow.engine import load_journal
from tests.fleet_fakes import FakeFleet, join_fake_gateway
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import notifications, run_turn, state

TOML = """
[providers.alpha]
via = "fleet"
node = "alpha"
agent = "claude"
idle_timeout = 0.4

[routing]
default = "alpha"
"""
HOLD = (
    "meta = {'name': 'hold', 'description': 'd'}\n\n"
    "async def main(args):\n    return await agent('hold on')\n"
)


async def _run(
    tmp_path: Path, drive: Callable[[FakeFleet, str], Awaitable[None]]
) -> tuple[dict[str, Any], FakeFleet, FlowFixture]:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "hold.py").write_text(HOLD, encoding="utf-8")
    fx = await boot_flow(tmp_path, "node", TOML, provider_options={"fleet_client_wait": 5.0})
    fleet = FakeFleet()
    client = await join_fake_gateway(fx.host, SESSION, fleet)
    try:
        assert await fx.wire.until(lambda: bool(state(fx.host, SESSION).get("activeClients")))
        turn_task = asyncio.create_task(run_turn(fx.wire, fx.chat, "t1", "/hold"))
        assert await fx.wire.until(lambda: bool(fleet.sessions))
        (uri,) = fleet.sessions
        await drive(fleet, uri)
        turn = await asyncio.wait_for(turn_task, 10)
    finally:
        await client.aclose()
        await fx.shut()
    return turn, fleet, fx


async def test_fleet_idlewatch_activity_keeps_a_busy_session(tmp_path: Path) -> None:
    async def busy(fleet: FakeFleet, uri: str) -> None:
        # 'working' throughout, far past idle_timeout, but making progress.
        for n in range(12):
            fleet.sessions[uri]["activity"] = n
            await asyncio.sleep(0.1)
        fleet.release(uri, "finished")

    turn, fleet, _ = await _run(tmp_path, busy)
    assert final_json(turn) == "finished"
    assert fleet.stopped == []


async def test_fleet_idlewatch_silent_session_times_out(tmp_path: Path) -> None:
    async def silent(fleet: FakeFleet, uri: str) -> None:
        fleet.sessions[uri]["activity"] = 7

    turn, fleet, _ = await _run(tmp_path, silent)
    assert final_json(turn) is None
    (uri,) = fleet.sessions
    assert fleet.stopped == [uri]
    assert any("no activity for 0.4s (idle_timeout)" in n for n in notifications(turn))
    (run,) = (tmp_path / "state" / "runs").iterdir()
    (record,) = load_journal(tmp_path / "state", run.name).agents
    assert (record.status, record.kind) == ("error", "timeout")


async def test_fleet_idlewatch_waiting_pauses_and_resumes(tmp_path: Path) -> None:
    async def waits(fleet: FakeFleet, uri: str) -> None:
        await asyncio.sleep(0.1)
        # A person takes well over idle_timeout to answer: paused, not expired.
        fleet.sessions[uri]["status"] = "waiting"
        await asyncio.sleep(1.0)
        fleet.sessions[uri]["status"] = "working"
        # Resuming restarts the clock: finishing within idle_timeout of it succeeds.
        await asyncio.sleep(0.2)
        fleet.release(uri, "answered")

    turn, fleet, _ = await _run(tmp_path, waits)
    assert final_json(turn) == "answered"
    assert fleet.stopped == []
