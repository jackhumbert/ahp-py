"""A :class:`~agent_host_server.transport.base.Transport` over one WebSocket."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from typing import Any

from agent_host_server.transport.base import TransportClosed

__all__ = ["WebSocketTransport"]


class WebSocketTransport:
    """One JSON-RPC message per text frame.

    The spec names no subprotocol a host must advertise, and the reference
    client negotiates none, so we do not require one.
    """

    def __init__(self, socket: Any) -> None:
        self._socket = socket

    async def send(self, message: Mapping[str, Any]) -> None:
        try:
            await self._socket.send(json.dumps(message))
        except Exception as exc:
            raise TransportClosed(str(exc)) from exc

    async def receive(self) -> dict[str, Any] | None:
        try:
            raw = await self._socket.recv()
        except Exception:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            # Malformed frames are dropped rather than killing the connection;
            # a peer that cannot frame JSON will fail its next request anyway.
            return await self.receive()
        return decoded if isinstance(decoded, dict) else None

    async def close(self) -> None:
        # Best-effort and idempotent: a socket already torn down is not an error.
        with contextlib.suppress(Exception):
            await self._socket.close()
