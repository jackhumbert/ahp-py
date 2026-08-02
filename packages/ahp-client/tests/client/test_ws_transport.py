"""The WebSocket transport, over a real loopback socket.

Only the behaviours that are easy to get wrong and expensive to get wrong: token
handling, clean-versus-abnormal close, and the malformed-frame path that the
sibling host implemented with unbounded recursion.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

from agent_host_client.client.errors import TransportError
from agent_host_client.ws.transport import _redact, _with_token

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_token_rides_the_query_string_because_browsers_cannot_set_headers() -> None:
    assert _with_token("ws://h:1", "abc") == "ws://h:1?tkn=abc"
    assert _with_token("ws://h:1?x=1", "abc") == "ws://h:1?x=1&tkn=abc"
    assert _with_token("ws://h:1", None) == "ws://h:1"


def test_a_token_is_percent_encoded() -> None:
    assert _with_token("ws://h:1", "a b&c=d") == "ws://h:1?tkn=a%20b%26c%3Dd"


def test_a_token_never_survives_into_an_exception_message() -> None:
    """`websockets` puts the URI in connection errors, so the failure path is
    exactly where a credential would leak into a log."""
    assert _redact("connect to ws://h:1?tkn=s3cret failed") == (
        "connect to ws://h:1?tkn=<redacted> failed"
    )
    assert _redact("ws://h:1?tkn=s3cret&x=1") == "ws://h:1?tkn=<redacted>&x=1"
    assert _redact("no token here") == "no token here"


async def _echo_server(handler: object) -> tuple[str, object]:
    server = await serve(handler, "127.0.0.1", 0)  # type: ignore[arg-type]
    port = next(iter(server.sockets)).getsockname()[1]
    return f"ws://127.0.0.1:{port}", server


async def test_round_trip_over_a_real_socket() -> None:
    async def handler(connection: ServerConnection) -> None:
        async for message in connection:
            await connection.send(message)

    url, server = await _echo_server(handler)
    from agent_host_client.ws.transport import WebSocketClientTransport

    transport = await WebSocketClientTransport.connect(url)
    await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    assert await asyncio.wait_for(transport.receive(), 2) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "ping",
        "params": {},
    }
    await transport.close()
    server.close()  # type: ignore[attr-defined]


async def test_a_clean_close_ends_the_stream_rather_than_raising() -> None:
    async def handler(connection: ServerConnection) -> None:
        await connection.close(1000, "done")

    url, server = await _echo_server(handler)
    from agent_host_client.ws.transport import WebSocketClientTransport

    transport = await WebSocketClientTransport.connect(url)
    assert await asyncio.wait_for(transport.receive(), 2) is None
    assert transport.close_info is not None
    assert transport.close_info.clean
    server.close()  # type: ignore[attr-defined]


async def test_an_abnormal_close_raises_so_the_supervisor_can_tell_them_apart() -> None:
    async def handler(connection: ServerConnection) -> None:
        await connection.close(1011, "internal error")

    url, server = await _echo_server(handler)
    from agent_host_client.ws.transport import WebSocketClientTransport

    transport = await WebSocketClientTransport.connect(url)
    with pytest.raises(TransportError) as caught:
        await asyncio.wait_for(transport.receive(), 2)
    assert caught.value.kind == "closed"
    assert transport.close_info is not None
    assert not transport.close_info.clean
    server.close()  # type: ignore[attr-defined]


async def test_a_malformed_frame_is_skipped_without_recursing() -> None:
    """The sibling host's transport did `return await self.receive()` here, so a
    peer streaming garbage exhausted the stack. This drives 2000 bad frames --
    well past any recursion limit -- and then a good one."""

    async def handler(connection: ServerConnection) -> None:
        for _ in range(2000):
            await connection.send("{not json")
        await connection.send(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}))
        await asyncio.sleep(0.1)

    url, server = await _echo_server(handler)
    from agent_host_client.ws.transport import WebSocketClientTransport

    transport = await WebSocketClientTransport.connect(url)
    assert await asyncio.wait_for(transport.receive(), 5) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {},
    }
    await transport.close()
    server.close()  # type: ignore[attr-defined]


async def test_a_json_scalar_is_not_a_json_rpc_message() -> None:
    async def handler(connection: ServerConnection) -> None:
        await connection.send("42")
        await connection.send(json.dumps([1, 2, 3]))
        await connection.send(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}))
        await asyncio.sleep(0.1)

    url, server = await _echo_server(handler)
    from agent_host_client.ws.transport import WebSocketClientTransport

    transport = await WebSocketClientTransport.connect(url)
    assert await asyncio.wait_for(transport.receive(), 2) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {},
    }
    await transport.close()
    server.close()  # type: ignore[attr-defined]


async def test_connect_failure_is_a_transport_error() -> None:
    from agent_host_client.ws.transport import WebSocketClientTransport

    with pytest.raises(TransportError) as caught:
        # Port 1 on loopback is not listening.
        await WebSocketClientTransport.connect("ws://127.0.0.1:1", open_timeout=1.0)
    assert caught.value.kind == "io"


async def test_connect_failure_redacts_the_token() -> None:
    from agent_host_client.ws.transport import WebSocketClientTransport

    with pytest.raises(TransportError) as caught:
        await WebSocketClientTransport.connect("ws://127.0.0.1:1", token="s3cret", open_timeout=1.0)
    assert "s3cret" not in str(caught.value)


async def test_the_transport_is_usable_by_the_client_end_to_end() -> None:
    """A handshake over a real socket, not just a frame round trip."""

    async def handler(connection: ServerConnection) -> None:
        async for message in connection:
            request = json.loads(message)
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "protocolVersion": "0.7.0",
                            "serverSeq": 7,
                            "snapshots": [],
                        },
                    }
                )
            )

    url, server = await _echo_server(handler)
    from agent_host_client.client import AhpClient
    from agent_host_client.ws.transport import WebSocketClientTransport

    transport = await WebSocketClientTransport.connect(url)
    client = AhpClient(transport)
    await client.connect()
    result = await asyncio.wait_for(client.initialize(client_id="c1"), 3)
    assert result["protocolVersion"] == "0.7.0"
    assert client.last_seen_server_seq == 7
    await client.shutdown()
    server.close()  # type: ignore[attr-defined]


async def test_websockets_is_not_imported_by_the_testing_kit() -> None:
    """Enforced by import-linter too, but this fails with a readable message."""
    import agent_host_client.testing as testing

    assert "websockets" not in getattr(testing, "__dict__", {})
    assert websockets is not None  # the import above is real, this is the contrast
