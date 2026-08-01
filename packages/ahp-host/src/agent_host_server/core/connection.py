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
from collections.abc import Mapping
from typing import Any

from agent_host_server.core.policy import ConnectionInfo
from agent_host_server.transport.base import Transport, TransportClosed

__all__ = ["Connection"]


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
        self._outbox: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        self._closed = False

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
        """Queue one outbound message. Never blocks, never reorders."""
        if not self._closed:
            self._outbox.put_nowait(message)

    def start_writer(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_loop())

    async def _write_loop(self) -> None:
        while True:
            message = await self._outbox.get()
            if message is None:
                return
            if self.wire_log is not None:
                self.wire_log.record("s2c", message, self.client_id or "?")
            try:
                await self.transport.send(message)
            except TransportClosed:
                return
            except Exception:
                continue

    async def drain(self) -> None:
        """Wait until every queued message has been handed to the transport.

        Used by tests and by orderly shutdown; the protocol itself never needs
        to know when the queue is empty.
        """
        while not self._outbox.empty():
            await asyncio.sleep(0)

    # ─── lifecycle ───────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._outbox.put_nowait(None)
        if self._writer is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer
        await self.transport.close()

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other
