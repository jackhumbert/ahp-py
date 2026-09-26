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

import asyncio
from typing import Any

import pytest

from ahp_host.core.connection import DEFAULT_OUTBOX_LIMIT, Connection

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


class Recorder:
    """A transport that drains and remembers, so the writer can actually run."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.closed = False

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    async def receive(self) -> Any:  # pragma: no cover - the writer never reads
        return None

    async def close(self) -> None:
        self.closed = True


class FailsOnSecondSend(Recorder):
    """A send that raises for ONE frame -- a serialization error, a transport
    fault not mapped to TransportClosed."""

    async def send(self, message: Any) -> None:
        if len(self.sent) == 1:
            raise ValueError("frame 2 refused to serialize")
        await super().send(message)


class TestOverflowActuallyTerminates:
    """The sentinel is enqueued into a queue that is BY DEFINITION full, so a
    bare `put_nowait(None)` deterministically raised QueueFull and was
    suppressed: the writer never learned it should stop, `close()`'s
    idempotence guard saw `_closed` and skipped the teardown, and the peer kept
    an open socket with a permanent, signal-free hole in its outbound stream --
    no code path fulfilled the enqueue docstring's promise."""

    async def test_the_writer_sees_the_sentinel_and_closes_the_transport(self) -> None:
        transport = Recorder()
        connection = Connection(transport, outbox_limit=4)
        for index in range(10):
            connection.enqueue({"jsonrpc": "2.0", "method": "action", "params": {"n": index}})
        assert connection.overflowed

        connection.start_writer()
        writer = connection._writer
        assert writer is not None
        await asyncio.wait_for(writer, timeout=1)

        assert transport.closed, "the overflowed connection never closed its transport"
        # The OLDEST queued frame was evicted to make room for the sentinel --
        # its delivery was already forfeit, and reconnect/replay is the
        # recovery. The rest deliver in order, and nothing follows the
        # sentinel.
        assert [m["params"]["n"] for m in transport.sent] == [1, 2, 3]

    async def test_close_still_tears_down_an_overflowed_connection(self) -> None:
        """`close()` must not treat "overflowed" as "already torn down": that
        is exactly the connection whose transport most needs closing."""
        transport = Recorder()
        connection = Connection(transport, outbox_limit=4)
        for index in range(10):
            connection.enqueue({"jsonrpc": "2.0", "method": "action", "params": {"n": index}})
        assert connection.overflowed

        await asyncio.wait_for(connection.close(), timeout=1)
        assert transport.closed


class TestASendFailureIsFatal:
    async def test_a_failed_send_closes_instead_of_skipping_the_frame(self) -> None:
        """Continuing past a failed send delivers every LATER frame while this
        one is missing -- the silent ordering hole invariant 10 exists to
        prevent, and the same trade as overflow: visible interruption over
        invisible corruption."""
        transport = FailsOnSecondSend()
        connection = Connection(transport, outbox_limit=8)
        for index in range(4):
            connection.enqueue({"jsonrpc": "2.0", "method": "action", "params": {"n": index}})

        connection.start_writer()
        writer = connection._writer
        assert writer is not None
        await asyncio.wait_for(writer, timeout=1)

        assert transport.closed, "the connection outlived a hole in its outbound stream"
        assert [m["params"]["n"] for m in transport.sent] == [0], (
            "frames were delivered past the one that failed"
        )
