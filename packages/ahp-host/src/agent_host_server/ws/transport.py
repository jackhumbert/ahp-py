"""A :class:`~agent_host_protocol.transport.base.Transport` over one WebSocket."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Mapping
from typing import Any, Final

from agent_host_protocol.transport.base import TransportClosed

__all__ = ["WebSocketTransport"]

_log = logging.getLogger(__name__)

#: Consecutive unusable frames before the connection is dropped. Generous --
#: a peer with an encoding bug should survive a burst -- but finite, because
#: "drop it and read the next one" against a peer sending only junk is an
#: unbounded loop driven entirely by the peer.
#:
#: Reset by the first usable frame, so this counts a RUN of junk, not a total.
_MALFORMED_LIMIT: Final = 64


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
        """The next frame this transport can make sense of, or None at EOF.

        A LOOP, not recursion. This used to `return await self.receive()` on a
        malformed frame, so a peer streaming junk drove one stack frame per
        frame -- measured, 5000 of them raise `RecursionError` inside the read
        task, which is a remote crash from unauthenticated input. 100 were
        fine, which is why it survived every hand test.
        """
        malformed = 0
        while True:
            try:
                raw = await self._socket.recv()
            except Exception:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                # Dropped rather than fatal: a peer that cannot frame JSON will
                # fail its next request anyway, and killing the connection would
                # turn one corrupt frame into a lost session.
                malformed += 1
                if malformed >= _MALFORMED_LIMIT:
                    # ...but not forever. A peer sending nothing BUT junk is
                    # either broken or hostile, and either way this loop is the
                    # only thing it is accomplishing.
                    _log.warning(
                        "closing a connection after %d consecutive malformed frames",
                        malformed,
                    )
                    return None
                continue
            if not isinstance(decoded, dict):
                # Valid JSON, not an object. A JSON-RPC message is an object;
                # anything else is as unusable as a parse failure and is counted
                # with it, or `[]` forever is the same denial of service.
                malformed += 1
                if malformed >= _MALFORMED_LIMIT:
                    _log.warning("closing a connection after %d unusable frames", malformed)
                    return None
                continue
            return decoded

    async def close(self) -> None:
        # Best-effort and idempotent: a socket already torn down is not an error.
        with contextlib.suppress(Exception):
            await self._socket.close()
