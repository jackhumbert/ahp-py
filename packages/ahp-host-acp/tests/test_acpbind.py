"""`python -m ahp_host_acp` off loopback: a token file is required, remote clients are let in.

Real loopback sockets and no agent process: a connection is only the WebSocket handshake
(the agent is spawned on a session's first turn, which these tests never start).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import websockets

from ahp_host_acp.__main__ import _parse_args, build_host, main, read_token, serve
from ahp_host_acp.config import load

TOKEN = "s3cret-token"


def _settings(tmp_path: Path, *flags: str) -> Any:
    argv = ["--root", str(tmp_path), "--command", "agent-that-never-runs"]
    argv += ["--state-dir", str(tmp_path / "state"), *flags]
    return load(_parse_args(argv))


def _token_file(tmp_path: Path, text: str = TOKEN) -> Path:
    path = tmp_path / "token"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def test_acpbind_off_loopback_without_a_token_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--root", str(tmp_path), "--command", "x", "--bind", "0.0.0.0"])
    assert "refusing to serve on 0.0.0.0 without --token-file" in str(info.value.code)


def test_acpbind_an_empty_token_file_is_refused(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, "--bind", "0.0.0.0", "--token-file", str(_token_file(tmp_path, ""))
    )
    with pytest.raises(SystemExit, match="is empty"):
        read_token(settings)


def test_acpbind_loopback_needs_no_token(tmp_path: Path) -> None:
    assert read_token(_settings(tmp_path, "--bind", "127.0.0.2")) is None
    token_file = str(_token_file(tmp_path))
    assert read_token(_settings(tmp_path, "--bind", "0.0.0.0", "--token-file", token_file)) == TOKEN


async def test_acpbind_serve_refuses_off_loopback_without_a_token(tmp_path: Path) -> None:
    host = build_host(_settings(tmp_path))
    with pytest.raises(ValueError, match="without a token"):
        await serve(host, bind="0.0.0.0", port=0, token=None, stop=asyncio.Event())
    await host.aclose()


async def _handshake(port: int, *, query: str = "", headers: dict[str, str] | None = None) -> bool:
    """Whether a client gets through the handshake and an `initialize` answer."""
    url = f"ws://127.0.0.1:{port}/{query}"
    try:
        async with asyncio.timeout(10):
            async with websockets.connect(url, additional_headers=headers or {}) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersions": ["1.0.0"], "clientId": "t"},
                        }
                    )
                )
                reply = json.loads(await ws.recv())
                return bool(reply.get("id") == 1)
    except (websockets.InvalidStatus, websockets.ConnectionClosed, OSError):
        return False


async def test_acpbind_all_interfaces_with_a_token(tmp_path: Path) -> None:
    """Bound to every interface: the token is required, by query or by header."""
    host = build_host(_settings(tmp_path))
    stop = asyncio.Event()
    ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        serve(host, bind="0.0.0.0", port=0, token=TOKEN, stop=stop, on_ready=ready.set_result)
    )
    try:
        port = await asyncio.wait_for(ready, 20)
        assert await _handshake(port, query=f"?tkn={TOKEN}")
        assert await _handshake(port, headers={"Authorization": f"Bearer {TOKEN}"})
        assert not await _handshake(port, query="?tkn=wrong")
        assert not await _handshake(port)
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)
