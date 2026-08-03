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

import asyncio
import contextlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ssl import SSLContext
from typing import Any, Final
from urllib.parse import quote

import websockets
from agent_host_protocol.transport import TransportClosed
from websockets.asyncio.client import ClientConnection

from agent_host_client.client.errors import TransportError, TransportErrorKind

__all__ = ["WebSocketClientTransport", "WebSocketCloseInfo"]

#: 1000 is a normal closure; 1005 means "no status", which is what a peer that
#: closes without a code produces and is not an error either.
_CLEAN_CLOSE_CODES: Final = frozenset({1000, 1005})

#: "Policy violation": the peer is refusing this client rather than failing.
_POLICY_VIOLATION: Final = 1008

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


#: How long a closing handshake may take before the socket is simply dropped.
#: `websockets` defaults to 10s, which is the right budget for a peer that is
#: still there and far too long for one that is not.
_CLOSE_TIMEOUT: Final = 2.0


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
        ssl: SSLContext | None = None,
        open_timeout: float = 10.0,
        max_size: int = 16 * 1024 * 1024,
    ) -> WebSocketClientTransport:
        """Open a connection.

        *token*, when given, is appended as ``?tkn=`` rather than a header,
        matching what VS Code's own transport does and what a browser client is
        limited to. It is percent-encoded, and it is never included in an
        exception message: ``websockets`` puts the URI in connection errors, so
        the failure path is exactly where a credential would leak.

        *ssl* is passed straight through. An ``SSLContext`` is the standard
        currency for a private CA or a client certificate, and it is the whole
        interface -- no ``verify=False`` convenience flag, no CA-bundle path
        parsing. Either would be this library taking a position on certificate
        trust, which is the embedder's to hold.
        """
        target = _with_token(url, token)
        try:
            socket = await websockets.connect(
                target,
                additional_headers=dict(headers) if headers else None,
                subprotocols=list(subprotocols) if subprotocols else None,  # type: ignore[arg-type]
                ssl=ssl,
                open_timeout=open_timeout,
                max_size=max_size,
            )
        except websockets.InvalidStatus as exc:
            # The handshake was answered, and the answer was no. Flattening this
            # into "io" is what makes an expired token retry forever.
            status = exc.response.status_code
            raise TransportError(
                "rejected",
                f"connect to {_redact(url)} refused with HTTP {status}",
                status=status,
            ) from exc
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

        A frame that will not decode to a JSON object **raises**
        ``json.JSONDecodeError``, one raise per frame. The client's read loop
        counts these into ``MalformedFrame`` diagnostics and closes past a
        threshold, the way the reference forces a 4002 close -- a transport
        that skipped them silently made both unreachable, so a peer streaming
        garbage was held open forever. Raising instead of retrying also cannot
        recurse: the sibling host's transport re-entered itself here, so that
        same peer exhausted the stack.
        """
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
            code = self._close_info.code
            # 1008 is "policy violation" -- the peer is refusing us, not
            # failing. Retrying it is the same doomed loop as an HTTP 401.
            kind: TransportErrorKind = "rejected" if code == _POLICY_VIOLATION else "closed"
            raise TransportError(
                kind,
                f"connection closed abnormally: {code} {self._close_info.reason}".rstrip(),
                close_code=code,
            ) from exc
        except OSError as exc:
            raise TransportError("io", f"receive failed: {exc}") from exc

        text = frame.decode("utf-8", errors="replace") if isinstance(frame, bytes) else frame
        decoded = json.loads(text)  # undecodable raises; the client counts it
        if isinstance(decoded, dict):
            return decoded
        # A valid JSON scalar or array is not a JSON-RPC message either, and
        # must hit the same accounting as undecodable text.
        raise json.JSONDecodeError(f"expected an object, got {type(decoded).__name__}", text, 0)

    async def close(self) -> None:
        """Best-effort, and BOUNDED.

        `websockets` runs a closing handshake with a default 10s timeout, and
        against a peer that is already gone -- which is every abnormal
        teardown -- it waits the whole thing out. `AhpClient.shutdown()` awaits
        this, so an embedder calling it in a `finally` after a dropped
        connection paid ten seconds per client, and `HostRuntime` paid it again
        on every reconnect. Measured at 9.99s before this bound.

        Past the deadline the socket is dropped rather than negotiated: the
        peer is not answering, and a polite close it will never read is worth
        nothing to either side.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._socket.close(), _CLOSE_TIMEOUT)


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
