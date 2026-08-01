"""Transport abstraction.

The core never imports a concrete transport. The interface is deliberately
symmetric -- the same shape serves a client and a server -- which is the single
best structural idea in the one existing third-party host: it makes WebSocket,
stdio and an in-process pair interchangeable, and gives end-to-end tests that
need no sockets at all.

The spec requires only an ordered, reliable, bidirectional, complete-message
stream. WebSocket is conventional rather than normative, and there is no
subprotocol name a host must advertise.
"""

from __future__ import annotations

from agent_host_server.transport.base import Transport, TransportClosed
from agent_host_server.transport.memory import MemoryTransport, memory_pair

__all__ = ["MemoryTransport", "Transport", "TransportClosed", "memory_pair"]
