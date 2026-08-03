"""The WebSocket transport, over a real loopback socket.

Only the behaviours that are easy to get wrong and expensive to get wrong: token
handling, clean-versus-abnormal close, and the malformed-frame path that the
sibling host implemented with unbounded recursion.
"""

from __future__ import annotations

import asyncio
import contextlib
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


async def test_a_malformed_frame_raises_rather_than_hiding_itself() -> None:
    """The transport parses eagerly and RAISES; `AhpClient._read_loop` counts
    and continues.

    The sibling host does the opposite -- it swallows bad frames inside
    `receive()` -- and this layering is the better one: the count becomes an
    observable `MalformedFrame` diagnostic instead of a silent skip, so a peer
    that is quietly corrupting half its traffic is something an operator can
    see. The cost is that the transport alone is not a filter, which is what
    this test pins.
    """

    async def handler(connection: ServerConnection) -> None:
        await connection.send("{not json")
        await connection.send(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}))
        await asyncio.sleep(0.1)

    url, server = await _echo_server(handler)
    from agent_host_client.ws.transport import WebSocketClientTransport

    transport = await WebSocketClientTransport.connect(url)
    with pytest.raises(json.JSONDecodeError):
        await asyncio.wait_for(transport.receive(), 2)
    # The socket is still good: one bad frame is not a closed connection.
    assert await asyncio.wait_for(transport.receive(), 2) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {},
    }
    await transport.close()
    server.close()  # type: ignore[attr-defined]


async def test_a_json_scalar_is_not_a_json_rpc_message() -> None:
    """Valid JSON, still not a message. It has to reach the same accounting as
    undecodable text, or `[]` forever is a peer-driven loop that costs nothing
    to send and never gets counted."""

    async def handler(connection: ServerConnection) -> None:
        await connection.send("42")
        await connection.send(json.dumps([1, 2, 3]))
        await connection.send(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}))
        await asyncio.sleep(0.1)

    url, server = await _echo_server(handler)
    from agent_host_client.ws.transport import WebSocketClientTransport

    transport = await WebSocketClientTransport.connect(url)
    for _ in range(2):
        with pytest.raises(json.JSONDecodeError):
            await asyncio.wait_for(transport.receive(), 2)
    assert await asyncio.wait_for(transport.receive(), 2) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {},
    }
    await transport.close()
    server.close()  # type: ignore[attr-defined]


async def test_a_flood_of_garbage_gives_up_instead_of_recursing() -> None:
    """The property the old test was really about, asserted where the loop now
    lives -- and against what the client actually does.

    The sibling host wrote this as `return await self.receive()`, so 5000 bad
    frames raised `RecursionError` inside its read task: a remote crash from
    unauthenticated input. This client counts instead, and past
    `MALFORMED_FRAME_LIMIT` it stops -- because a peer sending nothing but junk
    is not recovering, and holding the socket open only delays the in-flight
    requests that are going to fail anyway.

    So the assertion is not "it survives the flood". It is that the flood ends
    in a prompt, named, catchable state rather than a stack overflow or a hang.
    """
    from agent_host_client.client.client import MALFORMED_FRAME_LIMIT, AhpClient
    from agent_host_client.client.errors import ClientClosed
    from agent_host_client.ws.transport import WebSocketClientTransport

    async def handler(connection: ServerConnection) -> None:
        with contextlib.suppress(Exception):
            for _ in range(MALFORMED_FRAME_LIMIT * 4):
                await connection.send("{not json")
            # No trailing sleep: `server.close()` does not await handlers, so a
            # sleeping one is still pending when the loop tears down and the
            # test pays for it in wall clock. The client has already given up
            # by frame 8 -- long before these 32 are drained -- so nothing here
            # depends on the connection staying open.

    url, server = await _echo_server(handler)
    client = AhpClient(await WebSocketClientTransport.connect(url))
    await client.connect()
    try:
        # Waiting for the teardown rather than racing it with a request: the
        # flood crosses a real socket, so a `ping()` issued alongside it settles
        # on whichever happens first. Bounded tightly -- this takes about 10ms,
        # and a regression to hanging has to fail rather than merely be slow.
        async with asyncio.timeout(5):
            while client.connection_state.status == "connected":
                await asyncio.sleep(0.01)

        with pytest.raises(ClientClosed):
            await client.ping()
    finally:
        await client.shutdown()
        server.close()  # type: ignore[attr-defined]


async def test_a_burst_under_the_limit_is_survived() -> None:
    """The other half, and the one that makes the limit a policy rather than a
    hair trigger: a peer with an encoding hiccup keeps its session."""
    from agent_host_client.client.client import MALFORMED_FRAME_LIMIT, AhpClient
    from agent_host_client.ws.transport import WebSocketClientTransport

    async def handler(connection: ServerConnection) -> None:
        with contextlib.suppress(websockets.exceptions.ConnectionClosed):
            for _ in range(MALFORMED_FRAME_LIMIT - 1):
                await connection.send("{not json")
            async for raw in connection:
                request = json.loads(raw)
                if request.get("method") == "ping":
                    await connection.send(
                        json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}})
                    )

    url, server = await _echo_server(handler)
    client = AhpClient(await WebSocketClientTransport.connect(url))
    await client.connect()
    try:
        await asyncio.wait_for(client.ping(), 10)
    finally:
        await client.shutdown()
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


async def test_closing_against_a_dead_peer_does_not_wait_out_the_handshake() -> None:
    """`websockets` runs a closing handshake with a 10s default, and against a
    peer that is already gone it waits the whole thing out.

    `AhpClient.shutdown()` awaits `transport.close()`, so an embedder calling it
    in a `finally` after a dropped connection paid ten seconds per client -- and
    `HostRuntime` paid it again on every reconnect. Measured at 9.99s before the
    bound; a polite close the peer will never read is worth nothing to either
    side.
    """
    from agent_host_client.ws.transport import _CLOSE_TIMEOUT, WebSocketClientTransport

    async def handler(connection: ServerConnection) -> None:
        # Accept, then vanish without completing a handshake.
        with contextlib.suppress(Exception):
            await asyncio.sleep(30)

    url, server = await _echo_server(handler)
    transport = await WebSocketClientTransport.connect(url)
    started = asyncio.get_running_loop().time()
    await transport.close()
    elapsed = asyncio.get_running_loop().time() - started
    server.close()  # type: ignore[attr-defined]

    assert elapsed < _CLOSE_TIMEOUT + 1.0, f"close took {elapsed:.2f}s"


async def test_a_healthy_close_is_immediate() -> None:
    """The bound must not cost anything when the peer IS answering -- otherwise
    every ordinary shutdown pays for the pathological case."""
    from agent_host_client.ws.transport import WebSocketClientTransport

    async def handler(connection: ServerConnection) -> None:
        with contextlib.suppress(Exception):
            async for message in connection:
                await connection.send(message)

    url, server = await _echo_server(handler)
    transport = await WebSocketClientTransport.connect(url)
    started = asyncio.get_running_loop().time()
    await transport.close()
    elapsed = asyncio.get_running_loop().time() - started
    server.close()  # type: ignore[attr-defined]

    assert elapsed < 0.5, f"a clean close took {elapsed:.2f}s"
