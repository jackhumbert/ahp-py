"""Errors raised by this package.

`AcpError` carries a JSON-RPC error's `code`, `message` and `data`. The
subclasses are the cases a caller tells apart.
"""

from __future__ import annotations

from typing import Any, Final

METHOD_NOT_FOUND: Final = -32601
INTERNAL_ERROR: Final = -32603


class AcpError(Exception):
    """An error response from the agent, or a failure to talk to it."""

    def __init__(self, message: str = "", code: int = INTERNAL_ERROR, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class AgentExitedError(AcpError):
    """The agent's process ended (or its stdout closed) with requests in flight."""

    def __init__(self, message: str = "the ACP agent exited") -> None:
        super().__init__(message, INTERNAL_ERROR)


class MethodNotFoundError(AcpError):
    """Raise from a request handler for a method this client does not implement
    (answered with JSON-RPC -32601), or received as the agent's answer."""

    def __init__(self, method: str = "") -> None:
        super().__init__(f"Method not found: {method}" if method else "", METHOD_NOT_FOUND)
        self.method = method


class RequestTimeoutError(AcpError):
    """A request got no answer within its `timeout`."""

    def __init__(self, method: str, timeout: float) -> None:
        super().__init__(f"{method} timed out after {timeout:g}s", INTERNAL_ERROR)
        self.method = method
        self.timeout = timeout


class UnsupportedError(AcpError):
    """The agent did not advertise the capability a method needs; nothing was sent."""

    def __init__(self, method: str, capability: str) -> None:
        super().__init__(
            f"the agent does not advertise {capability}, needed for {method}", METHOD_NOT_FOUND
        )
        self.method = method
        self.capability = capability


class ProtocolVersionError(AcpError):
    """The agent answered `initialize` with a protocol version we did not ask for
    (only raised when `strict_version` is set)."""

    def __init__(self, requested: int, agreed: object) -> None:
        super().__init__(f"asked for ACP version {requested}, agent chose {agreed}", INTERNAL_ERROR)
        self.requested = requested
        self.agreed = agreed
