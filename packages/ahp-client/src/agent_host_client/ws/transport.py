"""A connecting WebSocket transport.

One JSON-RPC message per **text** frame. WebSocket is conventional rather than
normative -- the spec requires only an ordered, reliable, bidirectional stream of
complete messages and names no subprotocol -- so nothing here is advertised on
the handshake.

The shared ``Transport`` protocol carries decoded messages, not bytes, which is
what lets the whole suite run over an in-process pair with no serialisation at
all. That shape loses one thing a supervisor needs: whether a close was clean.
:class:`WebSocketCloseInfo` and the ``close_info`` property put it back without
widening a contract the sibling host also implements.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

import websockets
from agent_host_protocol.transport import TransportClosed
from websockets.asyncio.client import ClientConnection

from agent_host_client.client.errors import TransportError

__all__ = ["WebSocketClientTransport", "WebSocketCloseInfo"]

#: 1000 is a normal closure; 1005 means "no status", which is what a peer that
#: closes without a code produces and is not an error either.
_CLEAN_CLOSE_CODES: Final = frozenset({1000, 1005})

#: VS Code's connection-token query parameter. The token rides the URL because
#: browsers cannot set headers on a WebSocket handshake -- which is also why it
#: must never be logged.
_TOKEN_PARAM: Final = "tkn"


@dataclass(frozen=True, slots=True)
class WebSocketCloseInfo:
    code: int
    reason: str

    @property
    def clean(self) -> bool:
        return self.code in _CLEAN_CLOSE_CODES


class WebSocketClientTransport:
    """Connects to a host and carries decoded JSON-RPC messages."""

    def __init__(self, socket: ClientConnection) -> None:
        self._socket = socket
        self._close_info: WebSocketCloseInfo | None = None

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
        subprotocols: Sequence[str] | None = None,
        open_timeout: float = 10.0,
        max_size: int = 16 * 1024 * 1024,
    ) -> WebSocketClientTransport:
        """Open a connection.

        *token*, when given, is appended as ``?tkn=`` rather than a header,
        matching what VS Code's own transport does and what a browser client is
        limited to. It is percent-encoded, and it is never included in an
        exception message: ``websockets`` puts the URI in connection errors, so
        the failure path is exactly where a credential would leak.
        """
        target = _with_token(url, token)
        try:
            socket = await websockets.connect(
                target,
                additional_headers=dict(headers) if headers else None,
                subprotocols=list(subprotocols) if subprotocols else None,  # type: ignore[arg-type]
                open_timeout=open_timeout,
                max_size=max_size,
            )
        except Exception as exc:
            raise TransportError(
                "io", f"connect to {_redact(url)} failed: {_redact(str(exc))}"
            ) from exc
        return cls(socket)

    @classmethod
    def from_socket(cls, socket: ClientConnection) -> WebSocketClientTransport:
        """Wrap a socket somebody else opened (a tunnel, a test, a proxy)."""
        return cls(socket)

    @property
    def close_info(self) -> WebSocketCloseInfo | None:
        """How the peer closed, once it has. ``None`` while open.

        The supervisor reads this to tell "the host shut down on purpose" from
        "the network blinked", which are different reconnect decisions.
        """
        return self._close_info

    async def send(self, message: Mapping[str, Any]) -> None:
        try:
            await self._socket.send(json.dumps(message, separators=(",", ":")))
        except Exception as exc:
            raise TransportClosed(str(exc)) from exc

    async def receive(self) -> dict[str, Any] | None:
        """Next message, or ``None`` at a clean end of stream.

        A frame that will not parse is skipped, not fatal, and not recursed on:
        the sibling host's transport re-entered itself here, so a peer streaming
        garbage exhausted the stack. The client counts these and gives up at a
        threshold; the transport's job is only to keep going.
        """
        while True:
            try:
                frame = await self._socket.recv()
            except websockets.ConnectionClosed as exc:
                # `rcvd` is None when the peer vanished without a close frame;
                # 1006 is the code that condition is defined to report, and it
                # is not clean.
                received = exc.rcvd
                self._close_info = (
                    WebSocketCloseInfo(received.code, received.reason)
                    if received is not None
                    else WebSocketCloseInfo(1006, "")
                )
                if self._close_info.clean:
                    return None
                raise TransportError(
                    "closed", f"connection closed abnormally: {self._close_info.code}"
                ) from exc
            except OSError as exc:
                raise TransportError("io", f"receive failed: {exc}") from exc

            text = frame.decode("utf-8", errors="replace") if isinstance(frame, bytes) else frame
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                # Not our call whether this is fatal; skip and let the client
                # decide from how often it happens.
                continue
            if isinstance(decoded, dict):
                return decoded
            # A valid JSON scalar or array is not a JSON-RPC message.
            continue

    async def close(self) -> None:
        await self._socket.close()


def _with_token(url: str, token: str | None) -> str:
    if not token:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{_TOKEN_PARAM}={quote(token, safe='')}"


def _redact(text: str) -> str:
    """Strip a connection token out of anything about to become an exception."""
    marker = f"{_TOKEN_PARAM}="
    index = text.find(marker)
    if index == -1:
        return text
    end = index + len(marker)
    while end < len(text) and text[end] not in "&# ":
        end += 1
    return text[: index + len(marker)] + "<redacted>" + text[end:]
