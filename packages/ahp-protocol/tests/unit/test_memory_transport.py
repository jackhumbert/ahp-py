"""The in-memory transport pair, at the edges of its stream contract.

`Transport.receive`'s contract is "the next inbound message, or None once the
stream has ended" -- *persistently* None, because a supervisor loop calls
`receive()` again after handling a frame and must be able to tell "ended" from
"hung". The side that called `close()` always knew this; the side whose PEER
closed used to consume the single sentinel on the first call and then await an
empty queue with no writer left -- an asyncio hang, not an error.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_host_protocol.transport import MemoryTransport, TransportClosed, memory_pair


async def _receive_soon(transport: MemoryTransport) -> dict[str, Any] | None:
    """A `receive()` bounded by a timeout, so a regression fails a test in one
    second instead of hanging the suite forever."""
    return await asyncio.wait_for(transport.receive(), timeout=1.0)


def test_peer_close_yields_none_on_every_subsequent_receive() -> None:
    async def scenario() -> None:
        client, server = memory_pair()
        await client.close()
        # The surviving side's own `_closed` is False and only ONE sentinel was
        # ever enqueued, so the second call is the one that used to block.
        assert await _receive_soon(server) is None
        assert await _receive_soon(server) is None
        assert await _receive_soon(server) is None

    asyncio.run(scenario())


def test_own_close_yields_none_on_every_subsequent_receive() -> None:
    async def scenario() -> None:
        client, _server = memory_pair()
        await client.close()
        assert await _receive_soon(client) is None
        assert await _receive_soon(client) is None

    asyncio.run(scenario())


def test_messages_sent_before_close_drain_before_the_eof() -> None:
    async def scenario() -> None:
        client, server = memory_pair()
        await client.send({"seq": 1})
        await client.send({"seq": 2})
        await client.close()
        assert await _receive_soon(server) == {"seq": 1}
        assert await _receive_soon(server) == {"seq": 2}
        assert await _receive_soon(server) is None
        assert await _receive_soon(server) is None

    asyncio.run(scenario())


def test_both_sides_closing_still_terminates() -> None:
    async def scenario() -> None:
        client, server = memory_pair()
        await client.close()
        await server.close()
        # `server`'s inbox now holds two sentinels (one from each close); EOF
        # must stay latched past both.
        for _ in range(3):
            assert await _receive_soon(server) is None
        for _ in range(3):
            assert await _receive_soon(client) is None

    asyncio.run(scenario())


def test_send_after_peer_close_still_raises() -> None:
    async def scenario() -> None:
        client, server = memory_pair()
        await client.close()
        assert await _receive_soon(server) is None
        with pytest.raises(TransportClosed):
            await server.send({"seq": 1})

    asyncio.run(scenario())
