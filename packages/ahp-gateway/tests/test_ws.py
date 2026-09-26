"""Both edges over real sockets: surface -ws-> gateway -ws-> node."""

from __future__ import annotations

from ahp_client import connect
from ahp_host.ws.server import serve_websocket

from ahp_gateway.core import Gateway
from ahp_gateway.registry import NodeRecord, StaticInventory
from ahp_gateway.ws import NodeCredentials, WebSocketNodeConnector, serve_gateway
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
            gateway = Gateway(inventory, connector, everyone_is_a_dev)
            async with (
                serve_gateway(gateway, connection_token="surface-secret") as gateway_server,
                connect(
                    f"ws://127.0.0.1:{gateway_server.bound_port}/",
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
