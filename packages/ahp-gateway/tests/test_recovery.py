"""Reconnect and node recovery, against stock hosts and the stock supervised client."""

from __future__ import annotations

import re

from agent_host_client import AhpClient, RpcError
from agent_host_protocol import ROOT_URI

from agent_host_broker.registry import NodeRecord
from tests.fleet import (
    DEV,
    Fleet,
    echo_host,
    everyone_is_a_dev,
    generation,
    providers,
    reconnected,
)


async def test_reconnect_answers_with_fresh_snapshots_of_every_channel(fleet: Fleet) -> None:
    first = AhpClient(fleet.surface_transport())
    await first.connect()
    await first.initialize(client_id="c1", initial_subscriptions=[ROOT_URI])
    for provider in ("alpha", "beta"):
        await first.request("createSession", {"channel": f"{provider}:/s", "provider": provider})
        await first.request("subscribe", {"channel": f"{provider}:/s"})
    last_seen = first.last_seen_server_seq
    await first.shutdown()

    # A brand-new connection: nothing it has seen says which node owns what.
    second = AhpClient(fleet.surface_transport())
    await second.connect()
    result = await second.reconnect(
        client_id="c1",
        last_seen_server_seq=last_seen,
        subscriptions=[ROOT_URI, "alpha:/s", "beta:/s", "nobody:/s"],
    )
    await second.shutdown()

    assert result["type"] == "snapshot"
    resources = [snapshot["resource"] for snapshot in result["snapshots"]]
    # A channel no node has is absent: the snapshot arm's way of saying gone.
    assert resources == [ROOT_URI, "alpha:/s", "beta:/s"]
    assert all(snapshot["fromSeq"] >= last_seen for snapshot in result["snapshots"])
    assert providers_in(result["snapshots"][0]["state"]) == {"alpha", "beta"}


def providers_in(root: dict[str, object]) -> set[object]:
    agents = root.get("agents")
    assert isinstance(agents, list)
    return {agent.get("provider") for agent in agents}


async def test_a_surface_that_drops_resumes_its_sessions(fleet: Fleet) -> None:
    async with fleet.supervised_surface() as client:
        a = await client.create_session(provider="alpha")
        b = await client.create_session(provider="beta")
        assert "before" in (await b.prompt("before")).text

        before = generation(client)
        await fleet.drop_surface()
        await reconnected(client, before)

        assert "after on a" in (await a.prompt("after on a")).text
        assert "after on b" in (await b.prompt("after on b")).text
        listed = {item["resource"] for item in (await client.sessions())["items"]}
        assert listed == {a.uri, b.uri}


async def test_a_node_that_drops_is_redialed_and_the_surface_resyncs(fleet: Fleet) -> None:
    async with fleet.supervised_surface() as client:
        b = await client.create_session(provider="beta")
        assert "one" in (await b.prompt("one")).text

        before = generation(client)
        await fleet.connector.sever("node-b")
        # The broker redials node-b, finds it, and bounces the surface; the
        # client's own reconnect brings node-b's state back from snapshots.
        await reconnected(client, before)

        assert providers(client) == {"alpha", "beta"}
        assert "two" in (await b.prompt("two")).text


async def test_a_node_down_at_connect_joins_once_it_answers() -> None:
    hosts = {"node-a": echo_host("alpha"), "node-b": echo_host("beta")}
    fleet = Fleet(
        hosts,
        [NodeRecord("node-a", "m", DEV), NodeRecord("node-b", "m", DEV)],
        everyone_is_a_dev,
    )
    del fleet.connector.hosts["node-b"]  # unreachable for now
    try:
        async with fleet.supervised_surface() as client:
            assert providers(client) == {"alpha"}
            before = generation(client)
            fleet.connector.hosts["node-b"] = hosts["node-b"]
            await reconnected(client, before)
            assert providers(client) == {"alpha", "beta"}
            session = await client.create_session(provider="beta")
            assert "late" in (await session.prompt("late")).text
    finally:
        await fleet.aclose()


async def test_a_lost_nodes_session_is_refused_rather_than_rerouted() -> None:
    # A slow redial, so the window between loss and recovery can be observed.
    hosts = {"node-a": echo_host("alpha"), "node-b": echo_host("beta")}
    fleet = Fleet(
        hosts,
        [NodeRecord("node-a", "m", DEV), NodeRecord("node-b", "m", DEV)],
        everyone_is_a_dev,
        redial_backoff=(60.0, 60.0),
    )
    try:
        raw = AhpClient(fleet.surface_transport())
        await raw.connect()
        await raw.initialize(client_id="c1")
        await raw.request("createSession", {"channel": "beta:/s", "provider": "beta"})
        await fleet.connector.sever("node-b")
        # Until the broker notices the drop, a request may still be in flight
        # on the dying link; either way it must fail, and node-a must never be
        # the one to answer it.
        failure = None
        for _ in range(100):
            try:
                await raw.request("fetchTurns", {"channel": "beta:/s"})
            except RpcError as exc:
                failure = exc
                break
        assert failure is not None
        assert re.search(r"not connected|unavailable", failure.message)
        await raw.shutdown()
    finally:
        await fleet.aclose()
