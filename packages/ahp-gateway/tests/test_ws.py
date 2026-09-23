"""Both edges over real sockets: surface -ws-> broker -ws-> node."""

from __future__ import annotations

from agent_host_client import connect
from agent_host_server.ws.server import serve_websocket

from agent_host_broker.core import Broker
from agent_host_broker.registry import NodeRecord, StaticInventory
from agent_host_broker.ws import NodeCredentials, WebSocketNodeConnector, serve_broker
from tests.fleet import DEV, echo_host, everyone_is_a_dev


async def test_a_prompt_crosses_two_websocket_hops() -> None:
    node = echo_host("alpha")
    try:
        async with serve_websocket(node, connection_token="node-secret") as node_server:
            inventory = StaticInventory(
                [NodeRecord("node-a", f"ws://127.0.0.1:{node_server.bound_port}/", DEV)]
            )
            connector = WebSocketNodeConnector(
                lambda record, principal: NodeCredentials("node-secret")
            )
            broker = Broker(inventory, connector, everyone_is_a_dev)
            async with (
                serve_broker(broker, connection_token="surface-secret") as broker_server,
                connect(
                    f"ws://127.0.0.1:{broker_server.bound_port}/",
                    token="surface-secret",
                    reconnect=False,
                ) as client,
            ):
                assert [agent["provider"] for agent in client.agents()] == ["alpha"]
                session = await client.create_session(provider="alpha")
                result = await session.prompt("over the wire")
                assert "over the wire" in result.text
    finally:
        await node.aclose()
