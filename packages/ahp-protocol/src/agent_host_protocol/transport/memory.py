"""An in-process transport pair.

Lets the whole protocol suite -- handshake, subscriptions, sequencing, replay,
turns -- run with no sockets, no ports and no serialisation. Messages are
round-tripped through JSON on the way across so a test cannot accidentally pass
by sharing a mutable object between the two ends.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from agent_host_protocol.transport.base import TransportClosed

__all__ = ["MemoryTransport", "memory_pair"]


class MemoryTransport:
    def __init__(self, name: str = "memory") -> None:
        self.name = name
        self._inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._peer: MemoryTransport | None = None
        self._closed = False

    def _link(self, peer: MemoryTransport) -> None:
        self._peer = peer

    async def send(self, message: Mapping[str, Any]) -> None:
        if self._closed or self._peer is None or self._peer._closed:
            raise TransportClosed(f"{self.name}: peer is gone")
        # Serialise so the two ends never share a mutable structure -- that would
        # hide aliasing bugs the real wire would expose.
        self._peer._inbox.put_nowait(json.loads(json.dumps(message)))

    async def receive(self) -> dict[str, Any] | None:
        if self._closed and self._inbox.empty():
            return None
        return await self._inbox.get()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._inbox.put_nowait(None)
        if self._peer is not None and not self._peer._closed:
            self._peer._inbox.put_nowait(None)


def memory_pair() -> tuple[MemoryTransport, MemoryTransport]:
    """A linked (client, server) pair."""
    client = MemoryTransport("client")
    server = MemoryTransport("server")
    client._link(server)
    server._link(client)
    return client, server
