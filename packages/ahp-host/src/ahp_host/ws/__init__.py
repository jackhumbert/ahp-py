"""WebSocket transport.

Kept out of the core: `pip install ahp-host` gets you the protocol
layer with no dependencies, and `[ws]` adds this.

The spec is deliberately non-normative about transport -- it requires only an
ordered, reliable, bidirectional, complete-message stream, names no subprotocol,
and puts connection admission explicitly outside the wire protocol. So the
security posture here is ours to choose, and it follows the VS Code reference
host: a bearer connection token on the upgrade, rejected with HTTP 403, and
loopback by default.
"""

from __future__ import annotations

from ahp_host.ws.server import WebSocketServer, serve_websocket
from ahp_host.ws.transport import WebSocketTransport

__all__ = ["WebSocketServer", "WebSocketTransport", "serve_websocket"]
