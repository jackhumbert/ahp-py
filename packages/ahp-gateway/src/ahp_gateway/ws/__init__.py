"""Concrete WebSocket transports for the gateway's two edges.

Neither edge has its own socket code. The node edge dials with the client
sibling's transport, and the surface edge is served by the server sibling's
WebSocket server, so admission at the upgrade (the 403 on a bad token), the
loopback-by-default bind and the frame limits are the family's, not a second
copy.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from ssl import SSLContext
from typing import TYPE_CHECKING, Any, cast

from ahp_client.ws.transport import WebSocketClientTransport
from ahp_host.ws.server import WebSocketServer
from ahp_protocol import Transport

from ahp_gateway.core import Gateway
from ahp_gateway.registry import NodeRecord, Principal

if TYPE_CHECKING:
    from ahp_host import Host

__all__ = ["NodeCredentials", "WebSocketNodeConnector", "serve_gateway"]


@dataclass(frozen=True)
class NodeCredentials:
    """What the gateway presents to one node for one principal."""

    token: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


CredentialSource = Callable[[NodeRecord, Principal], NodeCredentials]


def _no_credentials(record: NodeRecord, principal: Principal) -> NodeCredentials:
    return NodeCredentials()


class WebSocketNodeConnector:
    """Dials a node's `NodeRecord.url` over WebSocket."""

    def __init__(
        self,
        credentials: CredentialSource = _no_credentials,
        *,
        ssl: SSLContext | None = None,
        open_timeout: float = 10.0,
    ) -> None:
        self._credentials = credentials
        self._ssl = ssl
        self._open_timeout = open_timeout

    async def connect(self, record: NodeRecord, principal: Principal) -> Transport:
        credentials = self._credentials(record, principal)
        return await WebSocketClientTransport.connect(
            record.url,
            headers=credentials.headers or None,
            token=credentials.token,
            ssl=self._ssl,
            open_timeout=self._open_timeout,
        )


@contextlib.asynccontextmanager
async def serve_gateway(
    gateway: Gateway,
    *,
    bind: str = "127.0.0.1",
    port: int = 0,
    connection_token: str | Callable[[str | None, Mapping[str, str]], bool] | None = None,
    allow_remote: bool = False,
) -> AsyncIterator[WebSocketServer]:
    """Serve `gateway` to the surfaces, exactly as a host is served.

    `WebSocketServer` is annotated as taking a concrete `Host` but only ever
    calls `serve(transport, peer=, headers=, token=)`, which `Gateway.serve`
    matches. The cast is that gap, and the fix belongs in the sibling: type
    the parameter as a protocol with that one method.
    """
    server = WebSocketServer(
        cast("Host", cast(Any, gateway)),
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
