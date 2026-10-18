"""A fleet tool call whose gateway vanishes, or never answers, is retried once, then fails.

`FleetDispatcher._call` watches each call: the offering client leaving (the host
reports it gone) or no answer within `reply_timeout` loses the call. A lost call
is tried once more on the client the gateway rejoins with; without one, or lost
again, `agent()` returns null with a log line saying why.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tests.fleet_fakes import FakeFleet, join_fake_gateway
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import notifications, run_turn, start_turn, state, wait_turn

TOML = """
[providers.alpha]
via = "fleet"
node = "alpha"
agent = "claude"
reply_timeout = 0.3

[routing]
default = "alpha"
"""
WORKFLOWS = {
    "hold": "meta = {'name': 'hold', 'description': 'd'}\n\n"
    "async def main(args):\n    return await agent('hold on')\n",
    "plain": "meta = {'name': 'plain', 'description': 'd'}\n\n"
    "async def main(args):\n    return await agent('x')\n",
}


async def boot(tmp_path: Path, *, rejoin_wait: float) -> FlowFixture:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    for name, source in WORKFLOWS.items():
        (user / f"{name}.py").write_text(source, encoding="utf-8")
    return await boot_flow(
        tmp_path, "node", TOML, provider_options={"fleet_client_wait": rejoin_wait}
    )


def clients(fx: FlowFixture) -> list[Mapping[str, Any]]:
    return list(state(fx.host, SESSION).get("activeClients") or [])


async def test_fleet_liveness_a_vanished_gateway_is_retried_on_the_rejoined_one(
    tmp_path: Path,
) -> None:
    fx = await boot(tmp_path, rejoin_wait=5.0)
    fleet = FakeFleet()
    first = await join_fake_gateway(fx.host, SESSION, fleet)
    second = None
    try:
        assert await fx.wire.until(lambda: bool(clients(fx)))
        await start_turn(fx.wire, fx.chat, "t1", "/hold")
        assert await fx.wire.until(lambda: bool(fleet.sessions))
        # Mid-call: only once a wait is actually in flight does the gateway go, or
        # the next wait would simply start on the rejoined gateway (no retry).
        assert await fx.wire.until(lambda: fleet.waiting > 0)
        await first.aclose()
        assert await fx.wire.until(lambda: not clients(fx))
        second = await join_fake_gateway(fx.host, SESSION, fleet)
        (uri,) = fleet.sessions
        fleet.release(uri, "done")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "done"
        # Not started twice: the wait was retried, on the same node session.
        assert len(fleet.starts) == 1
        notes = notifications(turn)
        assert any(
            "wait_for_sessions: the gateway offering the fleet tools left before answering; "
            "trying once more" in note
            for note in notes
        ), notes
    finally:
        if second is not None:
            await second.aclose()
        await fx.shut()


async def test_fleet_liveness_a_vanished_gateway_that_never_returns_fails_the_call(
    tmp_path: Path,
) -> None:
    fx = await boot(tmp_path, rejoin_wait=0.3)
    fleet = FakeFleet()
    client = await join_fake_gateway(fx.host, SESSION, fleet)
    try:
        assert await fx.wire.until(lambda: bool(clients(fx)))
        await start_turn(fx.wire, fx.chat, "t1", "/hold")
        assert await fx.wire.until(lambda: bool(fleet.sessions))
        await client.aclose()
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert turn["state"] == "complete"
        assert final_json(turn) is None
        notes = notifications(turn)
        assert any(
            "failed: wait_for_sessions: no gateway offers the fleet tools to this session "
            "(waited 0.3s)" in note
            for note in notes
        ), notes
    finally:
        await fx.shut()


async def test_fleet_liveness_a_call_with_no_answer_fails_after_the_reply_timeout(
    tmp_path: Path,
) -> None:
    fx = await boot(tmp_path, rejoin_wait=5.0)
    fleet = FakeFleet()
    fleet.hang_reads = True
    client = await join_fake_gateway(fx.host, SESSION, fleet)
    try:
        assert await fx.wire.until(lambda: bool(clients(fx)))
        turn = await run_turn(fx.wire, fx.chat, "t1", "/plain")
        assert final_json(turn) is None
        notes = notifications(turn)
        assert any(
            "read_session: no answer from the gateway within 0.3s; trying once more" in note
            for note in notes
        ), notes
        assert any(
            "failed: read_session: no answer from the gateway within 0.3s" in note for note in notes
        ), notes
    finally:
        await client.aclose()
        await fx.shut()
