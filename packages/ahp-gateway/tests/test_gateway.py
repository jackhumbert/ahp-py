"""The multiplexer end to end: stock client -> gateway -> stock hosts."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ahp_client import AhpClient, RpcError
from ahp_protocol import ROOT_URI

from ahp_gateway.registry import NodeRecord, Principal
from tests.fleet import DEV, Fleet, echo_host, everyone_is_a_dev, providers


async def test_one_login_sees_the_agents_of_every_admitted_node(fleet: Fleet) -> None:
    async with fleet.surface() as client:
        # `gamma` runs on a node devs are not admitted to; admission happens
        # before AHP, so the gateway never even dials it.
        assert providers(client) == {"alpha", "beta"}
    assert {node for node, _ in fleet.connector.dialed} == {"node-a", "node-b"}


async def test_the_surface_is_told_it_is_talking_to_one_host(fleet: Fleet) -> None:
    raw = AhpClient(fleet.surface_transport())
    await raw.connect()
    result = await raw.initialize(client_id="c1", initial_subscriptions=[ROOT_URI])
    await raw.shutdown()
    assert result["serverInfo"]["name"] == "ahp-gateway"
    (root,) = result["snapshots"]
    assert root["resource"] == ROOT_URI
    assert root["state"]["activeSessions"] == 0
    assert root["fromSeq"] <= result["serverSeq"]


async def test_a_turn_round_trips_through_the_gateway(fleet: Fleet) -> None:
    async with fleet.surface() as client:
        session = await client.create_session(provider="beta")
        result = await session.prompt("hello through the gateway")
        assert "hello through the gateway" in result.text


async def test_sessions_from_every_node_arrive_in_one_flat_list(fleet: Fleet) -> None:
    async with fleet.surface() as client:
        a = await client.create_session(provider="alpha")
        b = await client.create_session(provider="beta")
        listed = await client.sessions()
        assert {item["resource"] for item in listed["items"]} == {a.uri, b.uri}
        assert "nextCursor" not in listed

        # Paging one at a time walks both nodes with no repeats.
        first = await client.sessions(limit=1)
        second = await client.sessions(limit=1, cursor=first["nextCursor"])
        assert "nextCursor" not in second
        walked = [first["items"][0]["resource"], second["items"][0]["resource"]]
        assert sorted(walked) == sorted([a.uri, b.uri])


async def test_each_session_lives_on_the_node_that_offers_its_agent(fleet: Fleet) -> None:
    async with fleet.surface() as client:
        a = await client.create_session(provider="alpha")
        b = await client.create_session(provider="beta")
    async with fleet.direct("node-a") as node_a:
        on_a = {item["resource"] for item in (await node_a.sessions())["items"]}
    async with fleet.direct("node-b") as node_b:
        on_b = {item["resource"] for item in (await node_b.sessions())["items"]}
    assert on_a == {a.uri}
    assert on_b == {b.uri}


async def test_a_working_directory_picks_the_node_and_names_it() -> None:
    # Two nodes offering the same agent: only the directory can say which.
    fleet = Fleet(
        {"node-a": echo_host("echo"), "node-b": echo_host("echo")},
        [NodeRecord("node-a", "mem://a", DEV), NodeRecord("node-b", "mem://b", DEV)],
        everyone_is_a_dev,
    )
    try:
        async with fleet.surface() as client:
            assert [agent["provider"] for agent in client.agents()] == ["echo"]
            session = await client.create_session(
                provider="echo", working_directories=["ahp-file:///node-b/srv/repo"]
            )
            (listed,) = (await client.sessions())["items"]
            assert listed["resource"] == session.uri
            assert listed["workingDirectories"] == ["ahp-file:///node-b/srv/repo"]
        async with fleet.direct("node-b") as node_b:
            (on_b,) = (await node_b.sessions())["items"]
        # The node sees its own path space: the authority never reaches it.
        assert on_b["workingDirectories"] == ["file:///srv/repo"]
    finally:
        await fleet.aclose()


async def test_a_folderless_session_goes_to_the_first_node_offering_the_agent() -> None:
    # Two nodes, one agent, no folder: a plain chat. The inventory's first
    # connected node takes it rather than the request being refused.
    fleet = Fleet(
        {"node-a": echo_host("echo"), "node-b": echo_host("echo")},
        [NodeRecord("node-a", "mem://a", DEV), NodeRecord("node-b", "mem://b", DEV)],
        everyone_is_a_dev,
    )
    try:
        async with fleet.surface() as client:
            session = await client.create_session(provider="echo")
        async with fleet.direct("node-a") as node_a:
            on_a = {item["resource"] for item in (await node_a.sessions())["items"]}
        assert session.uri in on_a
    finally:
        await fleet.aclose()


async def test_a_file_on_another_node_is_refused(fleet: Fleet) -> None:
    raw = AhpClient(fleet.surface_transport())
    await raw.connect()
    await raw.initialize(client_id="c1")
    with pytest.raises(RpcError) as caught:
        await raw.request(
            "createSession",
            {
                "channel": "alpha:/1",
                "provider": "alpha",
                "workingDirectories": ["ahp-file:///nowhere/tmp"],
            },
        )
    await raw.shutdown()
    assert caught.value.code == -32602


async def test_an_unknown_principal_is_refused_at_the_handshake() -> None:
    fleet = Fleet({"node-a": echo_host("alpha")}, [NodeRecord("node-a", "m", DEV)], lambda _: None)
    try:
        raw = AhpClient(fleet.surface_transport())
        await raw.connect()
        with pytest.raises(RpcError) as caught:
            await raw.initialize(client_id="c1")
        await raw.shutdown()
        assert caught.value.code == -32009
        assert fleet.connector.dialed == []
    finally:
        await fleet.aclose()


async def test_an_unreachable_node_degrades_the_fleet_rather_than_refusing() -> None:
    fleet = Fleet(
        {"node-a": echo_host("alpha")},
        [NodeRecord("node-a", "m", DEV), NodeRecord("gone", "m", DEV)],
        everyone_is_a_dev,
    )
    try:
        async with fleet.surface() as client:
            assert providers(client) == {"alpha"}
            session = await client.create_session(provider="alpha")
            assert "still here" in (await session.prompt("still here")).text
    finally:
        await fleet.aclose()


class _Silent:
    """Wraps a connector so chosen nodes never answer, as a machine that is
    off (not refusing - silent) does: the dial hangs until the timeout."""

    def __init__(self, inner: Any, silent: set[str]) -> None:
        self.inner = inner
        self.silent = silent

    async def connect(self, record: NodeRecord, principal: Principal) -> Any:
        if record.node_id in self.silent:
            await asyncio.Event().wait()
        return await self.inner.connect(record, principal)


async def test_a_node_known_to_be_down_does_not_stall_every_handshake() -> None:
    fleet = Fleet(
        {"node-a": echo_host("alpha"), "node-b": echo_host("beta")},
        [NodeRecord("node-a", "m", DEV), NodeRecord("node-b", "m", DEV)],
        everyone_is_a_dev,
        connect_timeout=1.0,
        known_down_timeout=0.05,
        # No redial during the test: it would bounce the surfaces.
        redial_backoff=(60.0, 60.0),
    )
    silent = _Silent(fleet.connector, {"node-b"})
    fleet.gateway.connector = silent
    loop = asyncio.get_running_loop()
    try:
        started = loop.time()
        async with fleet.surface() as client:
            assert providers(client) == {"alpha"}
        # The first connection has to find out: the full timeout.
        assert loop.time() - started >= 1.0

        started = loop.time()
        async with fleet.surface() as client:
            assert providers(client) == {"alpha"}
        # Every later one already knows.
        assert loop.time() - started < 0.5

        # Back, and the next connection sees it: a node that answers is
        # never held to the short timeout's verdict.
        silent.silent.clear()
        async with fleet.surface() as client:
            assert providers(client) == {"alpha", "beta"}
        assert "node-b" not in fleet.gateway.down_nodes
    finally:
        await fleet.aclose()


async def test_actions_reach_the_surface_on_one_monotonic_sequence(fleet: Fleet) -> None:
    async with fleet.surface() as client:
        seqs: list[int] = []

        async def record() -> None:
            async for tagged in client.protocol.events():
                envelope: Any = getattr(tagged.event, "envelope", None)
                if envelope is not None:
                    seqs.append(envelope["serverSeq"])

        recorder = asyncio.create_task(record())
        a = await client.create_session(provider="alpha")
        b = await client.create_session(provider="beta")
        await asyncio.gather(a.prompt("one"), b.prompt("two"))
        recorder.cancel()
    assert seqs
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


async def test_the_nodes_see_the_surfaces_own_client_id(fleet: Fleet) -> None:
    principals: list[str] = []

    def authenticate(info: Any) -> Principal:
        principals.append(info.client_id)
        return Principal(info.client_id, DEV)

    fleet.gateway.authenticate = authenticate
    async with fleet.surface(client_id="vscode-42"):
        pass
    assert principals == ["vscode-42"]
    assert ("node-a", "vscode-42") in fleet.connector.dialed


async def test_the_root_lists_each_machine_and_the_agents_it_runs(fleet: Fleet) -> None:
    async with fleet.surface() as client:
        nodes = client.root["_meta"]["ahp-gateway/nodes"]
    # Each machine's own serverInfo rides along verbatim; the rest is the gateway's.
    for node in nodes:
        assert node.pop("serverInfo")["name"] == "ahp-host"
    assert nodes == [
        {
            "id": "node-a",
            "label": "node-a",
            "folder": "ahp-file:///node-a/",
            "connected": True,
            "agents": ["alpha"],
        },
        {
            "id": "node-b",
            "label": "node-b",
            "folder": "ahp-file:///node-b/",
            "connected": True,
            "agents": ["beta"],
        },
    ]


async def test_a_machine_label_comes_from_its_record() -> None:
    fleet = Fleet(
        {"studio": echo_host("echo")},
        [NodeRecord("studio", "mem://j", DEV, metadata={"label": "Studio's PC"})],
        everyone_is_a_dev,
    )
    try:
        async with fleet.surface() as client:
            (node,) = client.root["_meta"]["ahp-gateway/nodes"]
        assert node["label"] == "Studio's PC"
    finally:
        await fleet.aclose()


async def test_a_machines_own_config_and_server_info_ride_in_its_entry_not_the_root() -> None:
    from ahp_host import Host, HostInfo, LoopbackSingleUserPolicy
    from ahp_host.core.config import RootConfig
    from ahp_host.provider.echo import EchoProvider

    config = RootConfig(
        properties={"hostName": {"type": "string", "title": "Host name"}},
        values={"hostName": "studio-box"},
    )
    host = Host(
        EchoProvider(provider_id="echo"),
        LoopbackSingleUserPolicy(),
        info=HostInfo(name="copilotd", version="0.9.1"),
        root_config=config,
    )
    fleet = Fleet({"studio": host}, [NodeRecord("studio", "mem://j", DEV)], everyone_is_a_dev)
    try:
        async with fleet.surface() as client:
            (node,) = client.root["_meta"]["ahp-gateway/nodes"]
            # The fleet advertises no settings it could not dispatch (invariant 4)...
            assert "config" not in client.root
        # ...but the machine's own view survives, verbatim.
        assert node["serverInfo"] == {"name": "copilotd", "version": "0.9.1"}
        assert node["config"] == config.to_wire()
        assert "meta" not in node  # the sibling host sets no root _meta
    finally:
        await fleet.aclose()
