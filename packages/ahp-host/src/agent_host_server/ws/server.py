"""Serve a :class:`~agent_host_server.core.host.Host` over WebSocket.

Security posture, following the VS Code reference host's local endpoint:

* **loopback by default**, and binding anywhere else is a hard error unless the
  caller passes ``allow_remote=True`` -- an explicit acknowledgement that AHP
  defines no authentication and that a session is arbitrary code execution
  against a workspace;
* an optional **bearer connection token** on the upgrade, supplied as ``?tkn=``,
  rejected with **HTTP 403** exactly as VS Code does.

There is deliberately no module-level ``serve()`` convenience that binds a
socket without a :class:`~agent_host_server.core.policy.Policy`.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent_host_server.core.host import Host
from agent_host_server.ws.transport import WebSocketTransport

__all__ = ["WebSocketServer", "serve_websocket"]

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class WebSocketServer:
    def __init__(
        self,
        host: Host,
        *,
        bind: str = "127.0.0.1",
        port: int = 0,
        connection_token: str | None = None,
        allow_remote: bool = False,
    ) -> None:
        if bind not in _LOOPBACK and not allow_remote:
            raise ValueError(
                f"refusing to bind {bind!r}: AHP defines no authentication, and this "
                "library is single-trust-domain. Pass allow_remote=True only if you "
                "have read docs/research.md §8 and supplied a Policy that enforces "
                "your own access control."
            )
        self.host = host
        self.bind = bind
        self.port = port
        self.connection_token = connection_token
        self._server: Any = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("server is not running")
        sockets = getattr(self._server, "sockets", None) or []
        port = sockets[0].getsockname()[1] if sockets else self.port
        base = f"ws://{self.bind}:{port}"
        return f"{base}/?tkn={self.connection_token}" if self.connection_token else base

    def _authorize(self, path: str) -> bool:
        if self.connection_token is None:
            return True
        query = parse_qs(urlparse(path).query)
        supplied = query.get("tkn", [None])[0]
        return supplied is not None and secrets.compare_digest(supplied, self.connection_token)

    async def _handler(self, socket: Any) -> None:
        path = getattr(getattr(socket, "request", None), "path", "") or ""
        if not self._authorize(path):
            await socket.close(code=1008, reason="invalid connection token")
            return
        peer = str(getattr(socket, "remote_address", None) or "")
        await self.host.serve(WebSocketTransport(socket), peer=peer)

    async def _process_request(self, connection: Any, request: Any) -> Any:
        """Reject a bad token during the upgrade, with 403 -- as VS Code does."""
        del connection
        if self._authorize(getattr(request, "path", "") or ""):
            return None
        return request.respond(HTTPStatus.FORBIDDEN, "invalid connection token\n")

    async def start(self) -> None:
        import websockets

        self._server = await websockets.serve(
            self._handler,
            self.bind,
            self.port,
            process_request=self._process_request,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


@asynccontextmanager
async def serve_websocket(
    host: Host,
    *,
    bind: str = "127.0.0.1",
    port: int = 0,
    connection_token: str | None = None,
    allow_remote: bool = False,
) -> AsyncIterator[WebSocketServer]:
    server = WebSocketServer(
        host,
        bind=bind,
        port=port,
        connection_token=connection_token,
        allow_remote=allow_remote,
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
