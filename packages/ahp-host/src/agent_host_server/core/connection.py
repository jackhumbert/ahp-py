"""One client connection: an inbound read loop and a single outbound writer.

Two structural rules, both learned from defects in the one existing third-party
host:

* **Outbound is a queue drained by exactly one writer task.** Publishing is an
  O(1) non-blocking enqueue. Looping over connections with an unawaited send --
  what the prior art does -- can interleave writes and deliver envelopes out of
  order, which breaks every client's state mirror. Ordering *is* the correctness
  model.
* **The read loop dispatches requests as separate tasks.** Awaiting each handler
  inline means a slow `createSession` (which waits on an agent runtime) blocks a
  later `chat/toolCallComplete` on the same connection. Notifications targeting
  one channel are still handled in arrival order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from typing import Any

from agent_host_protocol.transport.base import Transport, TransportClosed

from agent_host_server.core.policy import ConnectionInfo

#: Frames a connection may have outstanding before it is closed. Generous for
#: a momentary stall -- a turn produces tens of frames, not thousands -- and
#: small enough that a wedged peer cannot grow host memory without bound.
DEFAULT_OUTBOX_LIMIT = 2048

__all__ = ["DEFAULT_OUTBOX_LIMIT", "Connection"]


_log = logging.getLogger(__name__)


class Connection:
    def __init__(
        self,
        transport: Transport,
        *,
        client_id: str = "",
        peer: str | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
        wire_log: Any = None,
        outbox_limit: int = DEFAULT_OUTBOX_LIMIT,
    ) -> None:
        self.transport = transport
        self.client_id = client_id
        self.peer = peer
        self.initialized = False
        self.protocol_version: str | None = None
        # Whatever the transport learned at admission time. For a WebSocket
        # that is the upgrade request's headers and its `?tkn=`; for another
        # transport it may be nothing at all.
        #
        # The library never interprets either. Which header carries a principal,
        # and whether to believe it, is the embedder's decision -- and a
        # forwarded header is only evidence if the socket can be reached
        # exclusively through the proxy that set it, which the library cannot
        # know and must not assume.
        self.headers = headers
        self.token = token
        self.wire_log = wire_log
        # BOUNDED. An unbounded queue meant a peer that stopped reading -- a
        # suspended laptop, a wedged renderer, a client behind a stalled proxy
        # -- accumulated frames in host memory for the life of that connection,
        # with no backpressure and no drop policy. Nothing on loopback; a slow
        # leak with an ordinary trigger on a host serving several people.
        self._outbox: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue(
            maxsize=max(1, outbox_limit)
        )
        self.overflowed = False
        self._writer: asyncio.Task[None] | None = None
        # Two flags, not one. `_closed` means "no more frames may be enqueued";
        # `_close_started` means "close() has begun tearing down". Overflow sets
        # only the first, because reusing one flag for both made `close()`'s
        # idempotence guard treat an overflowed connection as already torn down
        # -- so `serve`'s finally never closed the transport or joined the
        # writer, and the peer kept an open socket with a silent hole in its
        # outbound stream.
        self._closed = False
        self._close_started = False

    @property
    def info(self) -> ConnectionInfo:
        return ConnectionInfo(
            client_id=self.client_id,
            peer=self.peer,
            token=self.token,
            headers=self.headers,
        )

    # ─── outbound ────────────────────────────────────────────────────────

    def enqueue(self, message: Mapping[str, Any]) -> None:
        """Queue one outbound message. Never blocks, never reorders.

        On overflow the CONNECTION is closed rather than the frame dropped.

        That is the choice with a recovery path. The protocol already handles
        "you missed things": a client reconnects, and `reconnect` either replays
        the gap or answers with fresh snapshots and a `missing` list. Dropping
        a frame silently has no such path -- the client's state diverges from
        the host's with no signal that it happened, and invariant 10's ordering
        guarantee is exactly what makes a hole undetectable.

        Blocking is not available either: `enqueue` is called from inside the
        sequencer's critical section, so a slow peer would stall every other
        client on the host.

        So the peer is disconnected, sees it, and reconnects. The cost is a
        visible interruption; the alternative is invisible corruption.
        """
        if self._closed:
            return
        try:
            self._outbox.put_nowait(message)
        except asyncio.QueueFull:
            # Recorded before closing, so `Host.counters()` can show an
            # operator that this happened rather than leaving a mysterious
            # disconnect.
            self.overflowed = True
            _log.warning(
                "connection %s outbox full (%d frames); closing so the peer reconnects",
                self.client_id or self.peer or "?",
                self._outbox.maxsize,
            )
            self._closed = True
            self._signal_writer()

    def _signal_writer(self) -> None:
        """Put the stop sentinel where the writer WILL see it.

        The queue that needs the sentinel is by definition full -- overflow is
        the reason we are here -- so a bare `put_nowait(None)` deterministically
        raised `QueueFull`, was suppressed, and the writer never learned it
        should stop: no code path fulfilled the enqueue docstring's promise
        that the peer is disconnected. One queued frame is evicted to make
        room; the connection is being terminated, so its delivery was already
        forfeit, and the reconnect/replay path is how the peer recovers it.
        """
        try:
            self._outbox.put_nowait(None)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._outbox.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._outbox.put_nowait(None)

    def start_writer(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_loop())

    async def _write_loop(self) -> None:
        while True:
            message = await self._outbox.get()
            if message is None:
                break
            if self.wire_log is not None:
                self.wire_log.record("s2c", message, self.client_id or "?")
            try:
                await self.transport.send(message)
            except TransportClosed:
                return
            except Exception:
                # Fatal, not skipped. Continuing past a failed send delivers
                # every LATER frame while this one is missing -- the silent,
                # signal-free ordering hole that invariant 10 exists to
                # prevent, and the exact corruption the overflow policy trades
                # a visible disconnect to avoid. Same trade here.
                _log.exception(
                    "connection %s: send failed; closing so the peer reconnects",
                    self.client_id or self.peer or "?",
                )
                break
        # Reached on the stop sentinel or a failed send. Closing the TRANSPORT
        # is what makes the failure visible: it ends `transport.receive()` in
        # `Host.serve`, whose finally then runs the full teardown -- without
        # this, an overflowed peer kept an open socket that would never carry
        # another frame.
        with contextlib.suppress(Exception):
            await self.transport.close()

    async def drain(self) -> None:
        """Wait until every queued message has been handed to the transport.

        Used by tests and by orderly shutdown; the protocol itself never needs
        to know when the queue is empty.
        """
        while not self._outbox.empty():
            await asyncio.sleep(0)

    # ─── lifecycle ───────────────────────────────────────────────────────

    async def close(self) -> None:
        # Guarded by `_close_started`, NOT `_closed`: overflow sets `_closed`
        # to stop new frames, and reusing it here made this method a no-op for
        # exactly the connection that most needs its transport closed and its
        # writer joined.
        if self._close_started:
            return
        self._close_started = True
        self._closed = True
        self._signal_writer()
        if self._writer is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer
        await self.transport.close()

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other
