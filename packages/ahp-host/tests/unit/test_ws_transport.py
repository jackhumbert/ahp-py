"""The WebSocket transport's read path, which is where untrusted bytes land.

Everything above this has a reducer, a validation table or a policy in front of
it. This has a `json.loads` and a peer.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_host_server.ws.transport import _MALFORMED_LIMIT, WebSocketTransport

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Peer:
    """A socket that yields exactly what it was handed, then closes."""

    def __init__(self, *frames: Any) -> None:
        self.frames = list(frames)
        self.closed = False

    async def recv(self) -> Any:
        if not self.frames:
            raise ConnectionError("eof")
        return self.frames.pop(0)

    async def send(self, raw: str) -> None: ...

    async def close(self) -> None:
        self.closed = True


class TestMalformedFrames:
    async def test_junk_is_skipped_and_the_next_message_arrives(self) -> None:
        peer = _Peer("not json {", "[]", '{"jsonrpc": "2.0", "method": "ping"}')
        assert await WebSocketTransport(peer).receive() == {"jsonrpc": "2.0", "method": "ping"}

    async def test_a_flood_does_not_exhaust_the_stack(self) -> None:
        """This used to `return await self.receive()`, so a peer streaming junk
        drove one stack frame per frame. Measured: 100 were fine and 5000 raised
        `RecursionError` inside the read task -- a remote crash from
        unauthenticated input, invisible to every hand test."""
        peer = _Peer(*(["not json {"] * 4000), '{"jsonrpc": "2.0", "method": "ping"}')
        # Whatever it answers, it must not raise: below the limit it finds the
        # good frame, at or above it it gives up cleanly.
        result = await WebSocketTransport(peer).receive()
        assert result is None or result == {"jsonrpc": "2.0", "method": "ping"}

    async def test_a_run_of_junk_eventually_gives_up(self) -> None:
        """Dropping a bad frame and reading the next one is an unbounded loop
        driven entirely by the peer, so the run is bounded."""
        peer = _Peer(*(["not json {"] * (_MALFORMED_LIMIT + 5)))
        assert await WebSocketTransport(peer).receive() is None

    async def test_just_under_the_limit_still_recovers(self) -> None:
        """The bound must not be so tight that a peer with an encoding hiccup
        loses its session."""
        peer = _Peer(*(["not json {"] * (_MALFORMED_LIMIT - 1)), '{"ok": true}')
        assert await WebSocketTransport(peer).receive() == {"ok": True}

    async def test_the_count_is_per_call_not_cumulative(self) -> None:
        """A long-lived connection that hiccups once an hour must not eventually
        be closed for it."""
        frames: list[Any] = []
        for _ in range(4):
            frames += ["not json {"] * (_MALFORMED_LIMIT - 1) + ['{"ok": true}']
        transport = WebSocketTransport(_Peer(*frames))
        for _ in range(4):
            assert await transport.receive() == {"ok": True}

    async def test_valid_json_that_is_not_an_object_is_counted_too(self) -> None:
        """`[]` parses. It is still not a JSON-RPC message, and returning None
        per frame would leave the same peer-driven loop one level up."""
        peer = _Peer(*(["[1, 2, 3]"] * (_MALFORMED_LIMIT + 5)))
        assert await WebSocketTransport(peer).receive() is None


class TestTheOrdinaryPath:
    async def test_bytes_are_decoded(self) -> None:
        assert await WebSocketTransport(_Peer(b'{"ok": true}')).receive() == {"ok": True}

    async def test_invalid_utf8_does_not_kill_the_connection(self) -> None:
        """`decode("utf-8")` raised, which `recv`'s own `except` never caught --
        it is outside that try. Replaced rather than fatal, then counted as
        malformed like any other unreadable frame."""
        peer = _Peer(b"\xff\xfe not utf-8", b'{"ok": true}')
        assert await WebSocketTransport(peer).receive() == {"ok": True}

    async def test_eof_is_none(self) -> None:
        assert await WebSocketTransport(_Peer()).receive() is None
