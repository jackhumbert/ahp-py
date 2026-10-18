"""A fleet call cut off by a flow-host restart reattaches to its node session (plan §4.8).

The run is continued through `FlowSession.resume_turn`: by the host, once, after a
drain-restart, or on a client's `chat/turnResume` after a crash. A continued run's
fleet call whose journal start record names a node session waits on that session
again instead of starting another, unless the session is gone.
"""

from __future__ import annotations

from pathlib import Path

from ahp_client import Client

from tests.fleet_fakes import FakeFleet, join_fake_gateway
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json, restart_flow, store_for
from tests.hosting import notifications, start_turn, state, wait_turn
from tests.integration.test_flow_durability import wait_resumed

TOML = """
[providers.alpha]
via = "fleet"
node = "alpha"
agent = "claude"

[routing]
default = "alpha"
"""
WORKFLOW = (
    "meta = {'name': 'pair', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await agent('hold one')\n"
    "    b = await agent('two')\n"
    "    return {'a': a, 'b': b}\n"
)
OPTIONS = {"fleet_client_wait": 5.0}


async def _cut_off(base: Path, fleet: FakeFleet, *, forget: bool, drain: bool = True) -> str:
    """Start the run, wait for the first call to be working on its node, then stop the host.

    The fake gateway stays joined until the host goes, as a gateway does when the flow
    host's process stops under it: the stored session still lists it.
    """
    user = base / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "pair.py").write_text(WORKFLOW, encoding="utf-8")
    fx = await boot_flow(base, "node", TOML, store=store_for(base), provider_options=OPTIONS)
    client = await join_fake_gateway(fx.host, SESSION, fleet)
    assert await fx.wire.until(lambda: bool(state(fx.host, SESSION).get("activeClients")))
    await start_turn(fx.wire, fx.chat, "t1", "/pair")
    assert await fx.wire.until(lambda: bool(fleet.sessions))
    (uri,) = fleet.sessions
    if drain:
        assert fx.chat in await fx.host.drain(0.2)
    await fx.shut()
    await client.aclose()
    # Only an abort stops a node session: the host stopping leaves it running.
    assert fleet.stopped == []
    if forget:
        # Gone while the flow host was down (the node restarted, say).
        fleet.forget(uri)
    return uri


async def _continued(base: Path, fleet: FakeFleet) -> tuple[FlowFixture, Client]:
    """The restored host, rejoined by a gateway under another client id."""
    fx = await restart_flow(base, "node", TOML, provider_options=OPTIONS)
    # A restart closed every connection: the stored gateway is not offered as live.
    assert state(fx.host, SESSION).get("activeClients") == []
    client = await join_fake_gateway(fx.host, SESSION, fleet, client_id="fake-gateway-2")
    assert await fx.wire.until(lambda: bool(state(fx.host, SESSION).get("activeClients")))
    return fx, client


async def test_fleet_reattach_keeps_the_running_node_session(tmp_path: Path) -> None:
    fleet = FakeFleet()
    uri = await _cut_off(tmp_path, fleet, forget=False)
    fx, client = await _continued(tmp_path, fleet)
    try:
        fleet.release(uri, "done one")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {"a": "done one", "b": "alpha: two"}
        assert [s["prompt"] for s in fleet.starts] == ["hold one", "two"]
        notes = notifications(turn)
        assert any(f"reattached to node session {uri}" in note for note in notes), notes
    finally:
        await client.aclose()
        await fx.shut()


async def test_fleet_reattach_dispatches_again_when_the_session_is_gone(tmp_path: Path) -> None:
    fleet = FakeFleet()
    await _cut_off(tmp_path, fleet, forget=True)
    fx, client = await _continued(tmp_path, fleet)
    try:
        assert await fx.wire.until(lambda: len(fleet.sessions) == 2, timeout=5.0)
        fleet.release(list(fleet.sessions)[1], "done one")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {"a": "done one", "b": "alpha: two"}
        assert [s["prompt"] for s in fleet.starts] == ["hold one", "hold one", "two"]
        assert any("is gone; dispatching it again" in note for note in notifications(turn))
    finally:
        await client.aclose()
        await fx.shut()


async def test_fleet_reattach_after_a_crash_on_the_clients_resume(tmp_path: Path) -> None:
    """A crash resumes nothing by itself; `chat/turnResume` continues the run, reattached."""
    fleet = FakeFleet()
    uri = await _cut_off(tmp_path, fleet, forget=False, drain=False)
    fx, client = await _continued(tmp_path, fleet)
    try:
        failed = state(fx.host, fx.chat)["turns"][-1]
        assert (failed["id"], failed["state"]) == ("t1", "error")
        assert [s["prompt"] for s in fleet.starts] == ["hold one"]
        await fx.wire.dispatch(fx.chat, {"type": "chat/turnResume", "turnId": "t1"})
        fleet.release(uri, "done one")
        turn = await wait_resumed(fx)
        assert final_json(turn) == {"a": "done one", "b": "alpha: two"}
        assert [s["prompt"] for s in fleet.starts] == ["hold one", "two"]
    finally:
        await client.aclose()
        await fx.shut()
