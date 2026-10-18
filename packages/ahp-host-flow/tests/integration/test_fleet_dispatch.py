"""Fleet dispatch (plan §8 M7): `agent()` calls run on gateway nodes through the fleet tools."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ahp_client import Client

from ahp_host_flow.engine import load_journal
from tests.fleet_fakes import FakeFleet, join_fake_gateway
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import (
    cancel_turn,
    notifications,
    run_turn,
    start_turn,
    state,
    text_of,
    wait_turn,
    worker_chats,
)

TOML = """
[providers.alpha]
via = "fleet"
node = "alpha"
agent = "claude"

[providers.beta]
via = "fleet"
node = "beta"
agent = "claude"

[routing]
default = "alpha"
rules = [ { provider = "beta", label = "B*" } ]
"""
SCHEMA = "{'type': 'object', 'properties': {'n': {'type': 'integer'}}, 'required': ['n']}"


def workflow(name: str, body: str) -> str:
    return f"meta = {{'name': {name!r}, 'description': 'd'}}\n\nasync def main(args):\n{body}"


WORKFLOWS = {
    "split": workflow(
        "split",
        "    return await parallel([\n"
        "        lambda: agent('one', label='A'),\n"
        "        lambda: agent('two', label='B'),\n"
        "    ])\n",
    ),
    "good": workflow("good", f"    return await agent('{{\"n\": 2}}', schema={SCHEMA})\n"),
    "bad": workflow("bad", f'    return await agent(\'{{"n": "x"}}\', schema={SCHEMA})\n'),
    "spend": workflow(
        "spend",
        "    await agent('one')\n    await agent('two')\n    return budget.spent()\n",
    ),
    "hold": workflow("hold", "    return await agent('hold on', label='A')\n"),
    "plain": workflow("plain", "    return await agent('x')\n"),
}


async def boot(
    tmp_path: Path, *, join: bool = True
) -> tuple[FlowFixture, FakeFleet, Client | None]:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    for name, source in WORKFLOWS.items():
        (user / f"{name}.py").write_text(source, encoding="utf-8")
    fx = await boot_flow(tmp_path, "node", TOML)
    fleet = FakeFleet()
    client: Client | None = None
    if join:
        client = await join_fake_gateway(fx.host, SESSION, fleet)
        assert await fx.wire.until(lambda: bool(state(fx.host, SESSION).get("activeClients")))
        # The host hands the new client list to the session right after publishing it.
        await asyncio.sleep(0.1)
    return fx, fleet, client


async def close(fx: FlowFixture, client: Client | None) -> None:
    if client is not None:
        await client.aclose()
    await fx.shut()


async def test_fleet_dispatch_routes_calls_to_nodes(tmp_path: Path) -> None:
    fx, fleet, client = await boot(tmp_path)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/split")
        assert final_json(turn) == ["alpha: one", "beta: two"]
        assert sorted((s["node"], s["agent"]) for s in fleet.starts) == [
            ("alpha", "claude"),
            ("beta", "claude"),
        ]
        workers = worker_chats(fx.host, SESSION)
        assert sorted(w["title"] for w in workers) == ["A", "B"]
        for worker in workers:
            await fx.wire.request("subscribe", {"channel": worker["resource"]})
            assert "Node session: " in text_of(state(fx.host, str(worker["resource"])))
        (run,) = (tmp_path / "state" / "runs").iterdir()
        started = load_journal(tmp_path / "state", run.name).started
        assert {s.session for s in started} == set(fleet.sessions)
        assert len(fleet.sessions) == 2
    finally:
        await close(fx, client)


async def test_fleet_dispatch_structured_results(tmp_path: Path) -> None:
    fx, fleet, client = await boot(tmp_path)
    try:
        good = await run_turn(fx.wire, fx.chat, "t1", "/good")
        assert final_json(good) == {"n": 2}
        assert fleet.starts[0]["output_schema"]["required"] == ["n"]
        bad = await run_turn(fx.wire, fx.chat, "t2", "/bad")
        assert final_json(bad) is None
        assert any("failed" in note for note in notifications(bad))
    finally:
        await close(fx, client)


async def test_fleet_dispatch_reports_output_tokens_to_the_budget(tmp_path: Path) -> None:
    fx, _fleet, client = await boot(tmp_path)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/spend")
        assert final_json(turn) == 10
    finally:
        await close(fx, client)


async def test_fleet_dispatch_skip_stops_the_node_session(tmp_path: Path) -> None:
    fx, fleet, client = await boot(tmp_path)
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/hold")
        assert await fx.wire.until(lambda: len(worker_chats(fx.host, SESSION)) == 1)
        worker = str(worker_chats(fx.host, SESSION)[0]["resource"])
        await fx.wire.request("subscribe", {"channel": worker})
        assert await fx.wire.until(
            lambda: bool(fleet.sessions) and state(fx.host, worker).get("activeTurn") is not None
        )
        await cancel_turn(fx.wire, worker, state(fx.host, worker)["activeTurn"]["id"])
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) is None
        assert await fx.wire.until(lambda: bool(fleet.stopped))
        assert fleet.stopped == list(fleet.sessions)
    finally:
        await close(fx, client)


async def test_fleet_dispatch_abort_stops_the_node_session(tmp_path: Path) -> None:
    fx, fleet, client = await boot(tmp_path)
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/hold")
        assert await fx.wire.until(lambda: bool(fleet.sessions))
        await cancel_turn(fx.wire, fx.chat, "t1")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert turn["state"] == "cancelled"
        assert await fx.wire.until(lambda: bool(fleet.stopped), timeout=5.0)
        assert fleet.stopped[0] in fleet.sessions
    finally:
        await close(fx, client)


async def test_fleet_dispatch_without_a_gateway_fails_the_call(tmp_path: Path) -> None:
    fx, fleet, client = await boot(tmp_path, join=False)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/plain")
        assert final_json(turn) is None
        assert any("no gateway offers fleet tools" in note for note in notifications(turn))
        assert fleet.starts == []
    finally:
        await close(fx, client)
