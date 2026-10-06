"""What the hosts are gaining, through the gateway: content refs, chats, agents.

A diff's before and after are `ContentRef`s the node mints under a scheme of
its own; a chat can be created, moved and disposed; an agent's capabilities
and models change at runtime. Each is plain AHP, and each has to reach the
right machine - or be refused out loud - when there are several.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from ahp_client import AhpClient, RpcError
from ahp_client.client import ActionEvent
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.core.changesets import Changeset, FileChange
from ahp_host.provider.echo import EchoProvider
from ahp_protocol import ROOT_URI, Transport, memory_pair

from ahp_gateway.core.root import common_agent_capabilities, merge_root
from ahp_gateway.core.uris import learn_owned
from ahp_gateway.registry import NodeRecord, Principal
from tests.fleet import DEV, Fleet, HostConnector, everyone_is_a_dev
from tests.links import RecordingLink, connection


async def _raw(fleet: Fleet, **initialize: Any) -> AhpClient:
    raw = AhpClient(fleet.surface_transport())
    await raw.connect()
    await raw.initialize(client_id="c1", **initialize)
    return raw


def _host(provider: str, capabilities: Mapping[str, Any] | None = None) -> Host:
    return Host(
        EchoProvider(provider_id=provider, capabilities=capabilities), LoopbackSingleUserPolicy()
    )


def _two(a: Host, b: Host) -> Fleet:
    return Fleet(
        {"node-a": a, "node-b": b},
        [NodeRecord("node-a", "mem://a", DEV), NodeRecord("node-b", "mem://b", DEV)],
        everyone_is_a_dev,
    )


# ─── content refs ────────────────────────────────────────────────────────


async def _publish(host: Host, session: str, before: bytes, after: bytes) -> str:
    change = FileChange(uri="file:///work/main.py", before=before, after=after)
    return await host.publish_changeset(session, Changeset(label="changes"), [change])


async def _read(raw: AhpClient, uri: str) -> str:
    result = await raw.request(
        "resourceRead", {"channel": ROOT_URI, "uri": uri, "encoding": "utf-8"}
    )
    assert result["encoding"] == "utf-8"
    return str(result["data"])


async def test_a_diff_is_read_from_the_machine_that_made_it(fleet: Fleet) -> None:
    raw = await _raw(fleet)
    await raw.request("createSession", {"channel": "alpha:/s", "provider": "alpha"})
    await raw.request("createSession", {"channel": "beta:/s", "provider": "beta"})
    published = {
        "a": await _publish(fleet.hosts["node-a"], "alpha:/s", b"a was\n", b"a is\n"),
        "b": await _publish(fleet.hosts["node-b"], "beta:/s", b"b was\n", b"b is\n"),
    }
    refs: dict[str, dict[str, str]] = {}
    for name, changeset in published.items():
        result, _ = await raw.subscribe(changeset)
        (file,) = result["snapshot"]["state"]["files"]
        edit = file["edit"]
        # The node's private scheme reaches the surface verbatim; only the
        # file's own URI is the gateway's.
        assert edit["before"]["content"]["uri"].startswith("ahp-changeset-content:")
        assert edit["before"]["uri"].startswith("ahp-file:///node-")
        refs[name] = {side: edit[side]["content"]["uri"] for side in ("before", "after")}

    # Two machines, and nothing in the URI says which: the node that named it
    # answers. Before, this was refused as "cannot tell which node".
    assert await _read(raw, refs["a"]["before"]) == "a was\n"
    assert await _read(raw, refs["a"]["after"]) == "a is\n"
    assert await _read(raw, refs["b"]["before"]) == "b was\n"
    assert await _read(raw, refs["b"]["after"]) == "b is\n"
    await raw.shutdown()

    # A connection that never saw the diff - a reconnect, another gateway
    # instance - finds it by asking each machine, since a read changes nothing.
    fresh = await _raw(fleet)
    assert await _read(fresh, refs["b"]["after"]) == "b is\n"
    resolved = await fresh.request(
        "resourceResolve", {"channel": ROOT_URI, "uri": refs["b"]["before"]}
    )
    assert resolved["type"] == "file"
    with pytest.raises(RpcError) as caught:
        await _read(fresh, "ahp-changeset-content:/nobody-made-this")
    assert caught.value.code == -32008
    await fresh.shutdown()


async def test_a_content_ref_whose_machine_is_gone_is_not_asked_elsewhere(
    fleet: Fleet,
) -> None:
    raw = await _raw(fleet)
    await raw.request("createSession", {"channel": "beta:/s", "provider": "beta"})
    changeset = await _publish(fleet.hosts["node-b"], "beta:/s", b"x\n", b"y\n")
    result, _ = await raw.subscribe(changeset)
    uri = result["snapshot"]["state"]["files"][0]["edit"]["after"]["content"]["uri"]

    await fleet.connector.sever("node-b")
    refused = await _refusal(raw, uri)
    # Refused as gone - not `NotFound` from node-a, which was never asked:
    # another host may mint the same string for different bytes.
    assert refused.code == -32603
    assert "node-b" in refused.message
    await raw.shutdown()


async def _refusal(raw: AhpClient, uri: str) -> RpcError:
    async with asyncio.timeout(5):
        while True:
            try:
                await _read(raw, uri)
            except RpcError as exc:
                return exc
            await asyncio.sleep(0.01)


def test_content_refs_are_learned_beside_channels() -> None:
    state = {
        "resource": "ahp-chat:/c",
        "changesets": [
            {"uriTemplate": "ahp-changeset:/fixed"},
            {"uriTemplate": "ahp-cs:/{turnId}"},
        ],
        "files": [
            {
                "edit": {
                    "before": {
                        "uri": "ahp-file:///node-a/main.py",
                        "content": {"uri": "ahp-changeset-content:/abc"},
                    }
                }
            }
        ],
        "attachments": [{"uri": "file:///Users/me/notes.txt"}],
    }
    channels, content = learn_owned(state)
    # A variable-free template is itself the channel; one with a variable is
    # expanded by the client and found by asking.
    assert channels == {"ahp-chat:/c", "ahp-changeset:/fixed"}
    assert content == {"ahp-changeset-content:/abc"}


# ─── chats ───────────────────────────────────────────────────────────────


@pytest.fixture
async def chatty() -> AsyncIterator[Fleet]:
    many: dict[str, Any] = {"multipleChats": {}}
    fleet = _two(_host("alpha", many), _host("beta", many))
    yield fleet
    await fleet.aclose()


async def test_a_chat_moves_between_sessions_on_its_own_machine_only(chatty: Fleet) -> None:
    raw = await _raw(chatty)
    for channel, provider in (
        ("alpha:/one", "alpha"),
        ("alpha:/two", "alpha"),
        ("beta:/x", "beta"),
    ):
        await raw.request("createSession", {"channel": channel, "provider": provider})
    await raw.request("createChat", {"channel": "alpha:/one", "chat": "ahp-chat:/side"})
    result, _ = await raw.subscribe("ahp-chat:/side")
    assert result["snapshot"]["resource"] == "ahp-chat:/side"

    with pytest.raises(RpcError) as caught:
        await raw.request(
            "moveChat",
            {"channel": "ahp-chat:/side", "destination": {"kind": "session", "session": "beta:/x"}},
        )
    assert caught.value.code == -32602
    assert "another machine" in caught.value.message

    moved = await raw.request(
        "moveChat",
        {"channel": "ahp-chat:/side", "destination": {"kind": "session", "session": "alpha:/two"}},
    )
    assert moved == {"session": "alpha:/two"}
    await raw.request("disposeChat", {"channel": "ahp-chat:/side"})
    await raw.shutdown()


async def test_a_new_chat_routes_before_any_payload_names_it() -> None:
    a, b = RecordingLink("a"), RecordingLink("b")
    conn = connection(a, b)
    conn.owners.claim("b", {"beta:/s"})
    await conn._dispatch("createChat", {"channel": "beta:/s", "chat": "ahp-chat:/new"}, [])
    assert conn.owners.owner_of("ahp-chat:/new") == "b"
    assert a.requests == []


# ─── agents ──────────────────────────────────────────────────────────────


def test_a_shared_agent_offers_only_what_every_machine_can_keep() -> None:
    common = common_agent_capabilities(
        {
            "multipleChats": {"fork": True, "sideChat": True},
            "multipleWorkingDirectories": {"primaryReplacement": True},
            "futureThing": {"x": 1},
        },
        {
            "multipleChats": {"sideChat": True},
            "multipleWorkingDirectories": {"immutablePrimary": True, "primaryReplacement": True},
            "futureThing": {"x": 2},
        },
    )
    assert common == {
        "multipleChats": {"sideChat": True},
        # Allowing options need both; the restricting one holds if either says so.
        "multipleWorkingDirectories": {"primaryReplacement": True, "immutablePrimary": True},
    }
    assert common_agent_capabilities({"multipleChats": {}}, None) == {}


def test_a_capability_one_machine_lacks_is_not_offered() -> None:
    merged = merge_root(
        [
            {"agents": [{"provider": "echo", "capabilities": {"multipleChats": {"fork": True}}}]},
            {"agents": [{"provider": "echo"}]},
            {"agents": [{"provider": "solo", "capabilities": {"multipleChats": {}}}]},
        ]
    )
    shared, solo = merged["agents"]
    assert "capabilities" not in shared
    # A provider on one machine keeps its own, untouched.
    assert solo["capabilities"] == {"multipleChats": {}}


async def test_the_surface_sees_the_capabilities_both_machines_share() -> None:
    fleet = _two(
        _host("echo", {"multipleChats": {"fork": True, "sideChat": True}}),
        _host("echo", {"multipleChats": {"sideChat": True}}),
    )
    try:
        async with fleet.surface() as client:
            (agent,) = client.agents()
            assert agent["capabilities"] == {"multipleChats": {"sideChat": True}}
    finally:
        await fleet.aclose()


async def test_a_nodes_agent_change_reaches_the_surface_merged(fleet: Fleet) -> None:
    raw = await _raw(fleet, initial_subscriptions=[ROOT_URI])
    reader = raw.events()
    try:
        host = fleet.hosts["node-a"]
        (alpha,) = host.sequencer.state_of(ROOT_URI)["agents"]
        extra = {**alpha["models"][0], "id": "alpha-2", "name": "Alpha 2"}
        changed = {**alpha, "models": [*alpha["models"], extra]}
        await host.sequencer.publish(ROOT_URI, {"type": "root/agentsChanged", "agents": [changed]})

        async with asyncio.timeout(5):
            async for tagged in reader:
                event = tagged.event
                if isinstance(event, ActionEvent) and event.envelope["channel"] == ROOT_URI:
                    action = event.envelope["action"]
                    if action["type"] == "root/agentsChanged":
                        break
        # Re-merged, not relayed: the other machine's agent is still listed.
        by_provider = {agent["provider"]: agent for agent in action["agents"]}
        assert set(by_provider) == {"alpha", "beta"}
        assert [model["id"] for model in by_provider["alpha"]["models"]][-1] == "alpha-2"
    finally:
        await reader.aclose()
        await raw.shutdown()


# ─── what is passed on, and what is not ──────────────────────────────────


async def test_subscribe_options_reach_the_owning_node_and_every_probe() -> None:
    a, b = RecordingLink("a"), RecordingLink("b")
    conn = connection(a, b)
    conn.owners.claim("a", {"ahp-chat:/known"})
    options = {"view": {"turns": 2}, "delivery": {"maxLatencyMs": 50}}
    await conn._dispatch("subscribe", {"channel": "ahp-chat:/known", **options}, [])
    assert a.requests == [("subscribe", {"channel": "ahp-chat:/known", **options})]

    # Owner unknown: asked of each node, with the same options.
    await conn._dispatch("subscribe", {"channel": "ahp-chat:/unknown", **options}, [])
    assert a.requests[-1] == ("subscribe", {"channel": "ahp-chat:/unknown", **options})
    assert b.requests[-1] == ("subscribe", {"channel": "ahp-chat:/unknown", **options})


async def test_list_sessions_passes_on_what_it_does_not_page() -> None:
    a = RecordingLink("a", replies={"listSessions": {"items": []}})
    conn = connection(a)
    await conn._dispatch("listSessions", {"channel": ROOT_URI, "limit": 5, "filter": {"x": 1}}, [])
    ((method, params),) = a.requests
    assert method == "listSessions"
    assert params["filter"] == {"x": 1}
    assert params["limit"] == 5


class _Recording:
    """A host-side transport end that keeps what the host received."""

    def __init__(self, inner: Transport, seen: list[dict[str, Any]]) -> None:
        self.inner, self.seen = inner, seen

    async def send(self, message: Mapping[str, Any]) -> None:
        await self.inner.send(message)

    async def receive(self) -> dict[str, Any] | None:
        message = await self.inner.receive()
        if message is not None:
            self.seen.append(message)
        return message

    async def close(self) -> None:
        await self.inner.close()


class _RecordingConnector(HostConnector):
    def __init__(self, hosts: Mapping[str, Host]) -> None:
        super().__init__(hosts)
        self.seen: list[dict[str, Any]] = []

    async def connect(self, record: NodeRecord, principal: Principal) -> Transport:
        client_end, server_end = memory_pair()
        host = self.hosts[record.node_id]
        self.tasks.append(asyncio.create_task(host.serve(_Recording(server_end, self.seen))))
        return client_end


async def test_the_surfaces_locale_reaches_the_nodes_and_its_capabilities_do_not() -> None:
    fleet = Fleet(
        {"node-a": _host("alpha")}, [NodeRecord("node-a", "mem://a", DEV)], everyone_is_a_dev
    )
    connector = _RecordingConnector(fleet.hosts)
    fleet.gateway.connector = fleet.connector = connector
    try:
        raw = await _raw(fleet, locale="fr-FR", capabilities={"mcpApps": {}})
        await raw.shutdown()
        (handshake,) = [m["params"] for m in connector.seen if m.get("method") == "initialize"]
        # Option labels are localised by the node, so it needs the locale.
        assert handshake["locale"] == "fr-FR"
        # MCP App traffic would not cross the node edge whole, so the gateway
        # does not declare it on the surface's behalf (docs/plan.md §9).
        assert "capabilities" not in handshake
    finally:
        await fleet.aclose()
