"""The transport protocol every concrete transport implements."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

__all__ = ["Transport", "TransportClosed"]


class TransportClosed(Exception):
    """Raised by ``send`` once the peer has gone away."""


@runtime_checkable
class Transport(Protocol):
    """One ordered, reliable, bidirectional stream of complete JSON-RPC messages.

    Implementations carry decoded messages, not bytes: framing and encoding are
    the transport's business, and keeping them out of the core is what lets the
    protocol suite run over an in-process pair with no serialisation at all.
    """

    async def send(self, message: Mapping[str, Any]) -> None:
        """Deliver one message. Raises :class:`TransportClosed` if the peer is gone."""
        ...

    async def receive(self) -> dict[str, Any] | None:
        """Next inbound message, or ``None`` once the stream has ended."""
        ...

    async def close(self) -> None:
        """Close the stream. Idempotent."""
        ...
