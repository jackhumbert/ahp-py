"""A fleet call that fails after `start_session` stops its node session before it fails.

Only a call that has settled (read its result, or sent a stop) or detached (the host stopping)
leaves the node session alone; any other failure would leave it running and spending.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tests.fleet_fakes import FakeFleet, join_fake_gateway
from tests.flow_hosting import SESSION, final_json
from tests.hosting import notifications, run_turn, state
from tests.integration.test_fleet_dispatch import boot, close


class FailingWaits(FakeFleet):
    """`wait_for_sessions` fails once a node session is working."""

    async def wait(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise KeyError("the gateway lost track of the session")


class EmptyWaits(FakeFleet):
    """`wait_for_sessions` answers with no session row."""

    async def wait(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return {"sessions": [], "timed_out": True}


class FailingReads(FakeFleet):
    """`wait_for_sessions` reports the held session done, then `read_session` fails."""

    async def wait(self, args: Mapping[str, Any]) -> dict[str, Any]:
        uris = [str(u) for u in args["sessions"]]
        return {"sessions": [{"session": u, "status": "done", "done": True} for u in uris]}

    def read(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise KeyError("the session's result is unreadable")


async def _fails_and_stops(tmp_path: Path, fleet: FakeFleet, needle: str) -> None:
    fx, _, _ = await boot(tmp_path, join=False)
    client = await join_fake_gateway(fx.host, SESSION, fleet)
    try:
        assert await fx.wire.until(lambda: bool(state(fx.host, SESSION).get("activeClients")))
        turn = await run_turn(fx.wire, fx.chat, "t1", "/hold")
        assert final_json(turn) is None
        assert any(needle in note for note in notifications(turn)), notifications(turn)
        (uri,) = fleet.sessions
        assert fleet.stopped == [uri]
        assert fleet.sessions[uri]["status"] == "stopped"
    finally:
        await close(fx, client)


async def test_fleet_failstop_a_failed_wait_stops_the_node_session(tmp_path: Path) -> None:
    await _fails_and_stops(tmp_path, FailingWaits(), "the gateway lost track of the session")


async def test_fleet_failstop_a_wait_without_the_session_stops_it(tmp_path: Path) -> None:
    await _fails_and_stops(tmp_path, EmptyWaits(), "no session in the result")


async def test_fleet_failstop_a_failed_read_stops_the_node_session(tmp_path: Path) -> None:
    await _fails_and_stops(tmp_path, FailingReads(), "the session's result is unreadable")
