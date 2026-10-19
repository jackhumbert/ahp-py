"""WebSocket server options: the shared loopback check, the bearer validator,
and the keepalive / frame-limit knobs.

The loopback check used to live twice -- once here as a three-spelling set that
lacked 127.0.0.2, once in the flow CLI -- and the two disagreed (plan M13 item 0).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets

from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider
from ahp_host.ws import WebSocketServer, bearer_token_validator, is_loopback, serve_websocket

pytestmark = pytest.mark.anyio

ROOT_URI = "ahp:/root"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _host() -> Host:
    return Host(EchoProvider(), LoopbackSingleUserPolicy())


@pytest.mark.parametrize(
    "bind",
    ["localhost", "127.0.0.1", "127.0.0.2", "127.255.255.254", "::1"],
)
def test_wsopts_is_loopback(bind: str) -> None:
    assert is_loopback(bind) is True


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.168.1.10", "example.com", ""])
def test_wsopts_is_not_loopback(bind: str) -> None:
    assert is_loopback(bind) is False


def test_wsopts_server_accepts_any_loopback_bind() -> None:
    WebSocketServer(_host(), bind="127.0.0.2")
    with pytest.raises(ValueError, match="refusing to bind"):
        WebSocketServer(_host(), bind="0.0.0.0")
    WebSocketServer(_host(), bind="0.0.0.0", allow_remote=True)


def test_wsopts_bearer_validator() -> None:
    validate = bearer_token_validator("s3cret")
    assert validate("s3cret", {}) is True
    assert validate(None, {"authorization": "Bearer s3cret"}) is True
    assert validate(None, {"Authorization": "bearer s3cret"}) is True
    assert validate(None, {}) is False
    assert validate("nope", {}) is False
    assert validate(None, {"authorization": "Bearer nope"}) is False
    assert validate(None, {"authorization": "Basic s3cret"}) is False
    assert validate(None, {"authorization": "Bearer"}) is False
    assert validate(None, {"authorization": "Bearer s3crét"}) is False
    with pytest.raises(ValueError, match="non-empty string"):
        bearer_token_validator("")


class _FakeServer:
    sockets: tuple[Any, ...] = ()

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


async def test_wsopts_options_reach_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_serve(*args: Any, **kwargs: Any) -> _FakeServer:
        recorded.append(kwargs)
        return _FakeServer()

    monkeypatch.setattr(websockets, "serve", fake_serve)

    async with serve_websocket(_host(), ping_interval=5.0, max_size=4096) as server:
        assert (server.ping_interval, server.max_size) == (5.0, 4096)
    assert recorded[-1]["ping_interval"] == 5.0
    assert recorded[-1]["max_size"] == 4096

    async with serve_websocket(_host()):
        pass
    assert recorded[-1]["ping_interval"] == 20.0
    assert recorded[-1]["max_size"] == 1048576

    with pytest.raises(ValueError, match="ping_interval"):
        async with serve_websocket(_host(), ping_interval=0):
            pass
    with pytest.raises(ValueError, match="max_size"):
        async with serve_websocket(_host(), max_size=0):
            pass


def _big_initialize() -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersions": ["1.0.0"],
                "clientId": "c1",
                "channel": ROOT_URI,
                "initialSubscriptions": [ROOT_URI],
                "_meta": {"pad": "x" * (2 * 2**20)},
            },
        }
    )


async def _first_result(url: str) -> dict[str, Any] | None:
    """The first response frame, or ``None`` if the server closed instead."""
    try:
        async with websockets.connect(url, max_size=16 * 2**20) as client:
            await client.send(_big_initialize())
            while True:
                frame = json.loads(await asyncio.wait_for(client.recv(), 5))
                if "result" in frame:
                    return dict(frame)
    except websockets.ConnectionClosed:
        return None


async def test_wsopts_max_size_admits_a_large_frame() -> None:
    """2 MiB against a 4 MiB limit passes and against the 1 MiB default does not:
    a factor of two either way, far above any framing overhead."""
    async with serve_websocket(_host(), max_size=4 * 2**20) as server:
        reply = await _first_result(f"ws://127.0.0.1:{server.bound_port}/")
    assert reply is not None
    assert "result" in reply

    async with serve_websocket(_host()) as server:
        assert await _first_result(f"ws://127.0.0.1:{server.bound_port}/") is None
