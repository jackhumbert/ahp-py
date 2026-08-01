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

import logging
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent_host_server.core.host import Host
from agent_host_server.ws.transport import WebSocketTransport

__all__ = ["WebSocketServer", "serve_websocket"]

_log = logging.getLogger(__name__)

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _token_of(path: str) -> str | None:
    """VS Code's `?tkn=` from the upgrade path."""
    return parse_qs(urlparse(path).query).get("tkn", [None])[0]


def _headers_of(request: Any) -> Mapping[str, str] | None:
    """The upgrade request's headers, lower-cased, or ``None``.

    Copied rather than aliased: the live object belongs to the websockets
    library and outlives nothing in particular. Lower-cased because HTTP header
    names are case-insensitive and an embedder should not have to remember that
    about somebody else's proxy.
    """
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        return {str(k).lower(): str(v) for k, v in headers.raw_items()}
    except AttributeError:
        try:
            return {str(k).lower(): str(v) for k, v in dict(headers).items()}
        except Exception:  # pragma: no cover - defensive against a shape change
            return None


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
        #: The most recent handshake path, for diagnosing a refused client.
        self.last_handshake_path: str | None = None
        self._server: Any = None

    @property
    def bound_port(self) -> int:
        """The port actually bound, which differs from `port` when it was 0."""
        if self._server is None:
            raise RuntimeError("server is not running")
        sockets = getattr(self._server, "sockets", None) or []
        return int(sockets[0].getsockname()[1]) if sockets else self.port

    @property
    def url(self) -> str:
        """A full `ws://host:port/?tkn=...` URL.

        VS Code's own input parser accepts exactly this form and splits the
        token out into `connectionToken` itself (`tkn` is
        `connectionTokenQueryName` in `vs/base/common/network.ts`).
        """
        base = f"ws://{self.bind}:{self.bound_port}"
        return f"{base}/?tkn={self.connection_token}" if self.connection_token else base

    def _authorize(self, path: str) -> bool:
        if self.connection_token is None:
            return True
        supplied = _token_of(path)
        return supplied is not None and secrets.compare_digest(supplied, self.connection_token)

    async def _handler(self, socket: Any) -> None:
        # Admission already happened in `_process_request`; by here the upgrade
        # has completed, so this only wires the transport up -- and forwards
        # what the handshake saw, which is the only chance the Policy gets to
        # see it.
        peer = str(getattr(socket, "remote_address", None) or "")
        request = getattr(socket, "request", None)
        await self.host.serve(
            WebSocketTransport(socket),
            peer=peer,
            headers=_headers_of(request),
            token=_token_of(getattr(request, "path", "") or ""),
        )

    async def _process_request(self, connection: Any, request: Any) -> Any:
        """Reject a bad token during the upgrade, with 403 -- as VS Code does.

        `respond` lives on the *connection*, not the request. Getting that wrong
        raises inside the handshake, which websockets reports as a generic
        failure -- indistinguishable from a rejected token, and it aborts every
        connection including valid ones.
        """
        path = getattr(request, "path", "") or ""
        _log.debug("handshake path=%r", path)
        self.last_handshake_path = path
        if self._authorize(path):
            return None
        _log.warning("rejecting handshake: bad or missing connection token (path=%r)", path)
        return connection.respond(HTTPStatus.FORBIDDEN, "invalid connection token\n")

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
