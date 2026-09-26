"""The connecting WebSocket transport. Needs the `ws` extra."""

from __future__ import annotations

from agent_host_client.ws.transport import WebSocketClientTransport, WebSocketCloseInfo

__all__ = ["WebSocketClientTransport", "WebSocketCloseInfo"]
