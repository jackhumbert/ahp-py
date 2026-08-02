"""The outbox is bounded, and overflow closes the connection.

`Connection._outbox` was an unbounded `asyncio.Queue` with no drop policy. A
peer that stops reading -- a suspended laptop, a wedged renderer, a client
behind a stalled proxy -- accumulated frames in host memory for the life of
that connection. Nothing on loopback; a slow leak with an ordinary trigger on a
host serving several people.

Closing rather than dropping is the choice with a RECOVERY PATH. The protocol
already handles "you missed things": the peer reconnects, and `reconnect`
either replays the gap or answers with fresh snapshots and a `missing` list.
A silently dropped frame has no such path, and invariant 10's ordering
guarantee is exactly what makes a hole undetectable.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_host_server.core.connection import DEFAULT_OUTBOX_LIMIT, Connection

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Deaf:
    """A transport that never drains. The failure mode, exactly."""

    async def send(self, message: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("the writer should not be running in these tests")

    async def receive(self) -> Any:  # pragma: no cover
        return None

    async def close(self) -> None:
        return None


class TestTheBound:
    async def test_a_peer_that_never_reads_cannot_grow_host_memory(self) -> None:
        connection = Connection(Deaf(), outbox_limit=8)
        for index in range(500):
            connection.enqueue({"jsonrpc": "2.0", "method": "action", "params": {"n": index}})
        assert connection.overflowed
        # Bounded by the limit plus the sentinel that stops the writer.
        assert connection._outbox.qsize() <= 8 + 1

    async def test_overflow_closes_rather_than_dropping(self) -> None:
        """A dropped frame is invisible; a closed connection is not, and the
        peer's reconnect is the protocol's own recovery."""
        connection = Connection(Deaf(), outbox_limit=4)
        for _ in range(50):
            connection.enqueue({"jsonrpc": "2.0", "method": "action"})
        assert connection.overflowed

        # And it stays closed: further frames are not queued behind the
        # sentinel, which would make the shutdown never happen.
        before = connection._outbox.qsize()
        connection.enqueue({"jsonrpc": "2.0", "method": "action"})
        assert connection._outbox.qsize() == before

    async def test_a_healthy_connection_is_untouched(self) -> None:
        connection = Connection(Deaf(), outbox_limit=DEFAULT_OUTBOX_LIMIT)
        for index in range(100):
            connection.enqueue({"jsonrpc": "2.0", "method": "action", "params": {"n": index}})
        assert not connection.overflowed
        assert connection._outbox.qsize() == 100

    async def test_the_default_is_generous(self) -> None:
        """A turn produces tens of frames, not thousands. The default must not
        disconnect an ordinary client that paused for a moment."""
        assert DEFAULT_OUTBOX_LIMIT >= 1024
