"""`python -m ahp_host_flow` served off loopback and behind a header-forwarding proxy.

Real loopback sockets, no live model: the inner provider is the echo fake.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import ahp_client
import pytest
from ahp_client import TransportError, TurnCompleted, TurnFailed
from ahp_client.ws.transport import WebSocketClientTransport
from ahp_host import Host
from ahp_host.core import ConnectionInfo, LoopbackSingleUserPolicy
from ahp_protocol.channels import ROOT_URI

from ahp_host_flow.__main__ import _parse_args, _serve, build_host, main
from ahp_host_flow.config import load_config
from tests.fleet_hosting import final_json_of

ECHO_TOML = """\
state_dir = "state"

[providers.echo]
load = "ahp_host.provider.echo:EchoProvider"
"""

TOKEN = "s3cret"
WORKFLOW = (
    "meta = {'name': 'proxyflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('hi', label='a')\n"
)


def write_config(tmp_path: Path) -> Path:
    """A flow.toml serving the `proxyflow` workflow over the stripped echo provider."""
    workflows = tmp_path / "wf"
    workflows.mkdir()
    (workflows / "proxyflow.py").write_text(WORKFLOW, encoding="utf-8")
    toml = tmp_path / "flow.toml"
    toml.write_text(
        f"state_dir = '{(tmp_path / 'state').as_posix()}'\n"
        "\n"
        "[providers.echo]\n"
        'load = "tests.flow_fakes:StrippedEchoProvider"\n'
        "\n"
        "[workflows]\n"
        f"dirs = ['{workflows.as_posix()}']\n"
        "watch_interval = 0\n",
        encoding="utf-8",
    )
    return toml


class Serving:
    """A real `_serve` running as a task, with the port it bound."""

    def __init__(self, host: Host, bind: str, token: str | None, **options: Any) -> None:
        self.stop = asyncio.Event()
        self.ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(
            _serve(
                host,
                bind=bind,
                port=0,
                token=token,
                stop=self.stop,
                on_ready=self.ready.set_result,
                **options,
            )
        )

    async def port(self) -> int:
        waiting: set[asyncio.Future[Any]] = {self.ready, self.task}
        done, _ = await asyncio.wait(waiting, timeout=20, return_when=asyncio.FIRST_COMPLETED)
        if self.task in done:
            self.task.result()
        return await asyncio.wait_for(self.ready, timeout=1)

    async def close(self) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=20)


async def run_proxyflow(port: int, token: str | None) -> str:
    """Connect to *port*, run `/proxyflow` on a new session and return its JSON result."""
    async with asyncio.timeout(20):
        async with (
            ahp_client.connect(f"ws://127.0.0.1:{port}/", token=token, reconnect=False) as client,
            await client.create_session(provider="flow", uri="flow:/proxy-1") as session,
        ):
            async for event in session.prompt("/proxyflow", idle_timeout=None):
                if isinstance(event, TurnCompleted):
                    return str(final_json_of(event))
                if isinstance(event, TurnFailed):
                    raise AssertionError(event.reason)
    raise AssertionError("the turn never completed")


async def refused(port: int, token: str | None) -> bool:
    """Whether connecting to *port* with *token* is turned away."""
    try:
        async with asyncio.timeout(20):
            async with ahp_client.connect(f"ws://127.0.0.1:{port}/", token=token, reconnect=False):
                return False
    except (TransportError, OSError):
        return True


class HeaderRecordingPolicy(LoopbackSingleUserPolicy):
    def __init__(self) -> None:
        self.headers: list[Mapping[str, str]] = []

    def authorize_connection(self, info: ConnectionInfo) -> bool:
        self.headers.append(dict(info.headers or {}))
        return True


class Relay:
    """A TCP relay in front of *target* that adds headers to each upgrade request."""

    def __init__(self, target: int, extra: bytes) -> None:
        self.target = target
        self.extra = extra
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            head = head[:-2] + self.extra + b"\r\n"
            up_reader, upstream = await asyncio.open_connection("127.0.0.1", self.target)
            upstream.write(head)
            await upstream.drain()
            pumps = [
                asyncio.create_task(self._pump(reader, upstream)),
                asyncio.create_task(self._pump(up_reader, writer)),
            ]
            self.tasks.update(pumps)
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pump in pumps:
                    pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
                self.tasks.difference_update(pumps)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for w in (writer, upstream):
                if w is not None:
                    w.close()

    @staticmethod
    async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while chunk := await src.read(65536):
                dst.write(chunk)
                await dst.drain()


def test_flowproxy_127_0_0_2_is_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[tuple[str, str | None]] = []

    async def fake_serve(
        host: object, *, bind: str, port: int, token: str | None, **_: Any
    ) -> None:
        served.append((bind, token))

    monkeypatch.setattr("ahp_host_flow.__main__._serve", fake_serve)
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    main(["--config", str(toml), "--bind", "127.0.0.2"])
    assert served == [("127.0.0.2", None)]


def test_flowproxy_flags_reach_serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[float | None, int | None]] = []

    async def fake_serve(
        host: object,
        *,
        bind: str,
        port: int,
        token: str | None,
        ping_interval: float | None = 20.0,
        max_size: int | None = 16 * 1024 * 1024,
        **_: Any,
    ) -> None:
        seen.append((ping_interval, max_size))

    monkeypatch.setattr("ahp_host_flow.__main__._serve", fake_serve)
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    base = ["--config", str(toml), "--no-store"]
    main([*base, "--ping-interval", "7.5", "--max-size", "4096"])
    main([*base, "--ping-interval", "0", "--max-size", "0"])
    main(base)
    assert seen == [(7.5, 4096), (None, None), (20.0, 16 * 1024 * 1024)]
    with pytest.raises(SystemExit) as info:
        main([*base, "--ping-interval", "-1"])
    assert info.value.code == "ahp-host-flow: --ping-interval must be >= 0"
    with pytest.raises(SystemExit) as info:
        main([*base, "--max-size", "-1"])
    assert info.value.code == "ahp-host-flow: --max-size must be >= 0"
    assert len(seen) == 3


async def test_flowproxy_serve_binds_all_interfaces_with_token(tmp_path: Path) -> None:
    host = build_host(load_config(write_config(tmp_path)), store=False)
    serving = Serving(host, "0.0.0.0", TOKEN)
    try:
        port = await serving.port()
        assert (await run_proxyflow(port, TOKEN)).endswith("hi")
        assert await refused(port, "wrong")
    finally:
        await serving.close()


async def test_flowproxy_serve_refuses_off_loopback_without_a_token(tmp_path: Path) -> None:
    host = build_host(load_config(write_config(tmp_path)), store=False)
    with pytest.raises(ValueError, match="without a token"):
        await _serve(host, bind="0.0.0.0", port=0, token=None, stop=asyncio.Event())
    await host.aclose()


async def test_flowproxy_relay_forwards_header_token(tmp_path: Path) -> None:
    host = build_host(load_config(write_config(tmp_path)), store=False)
    policy = HeaderRecordingPolicy()
    host.policy = policy
    serving = Serving(host, "127.0.0.1", TOKEN)
    forwarded = b"X-Forwarded-For: 203.0.113.7\r\n"
    with_token = Relay(0, forwarded + f"Authorization: Bearer {TOKEN}\r\n".encode())
    without_token = Relay(0, forwarded)
    try:
        port = await serving.port()
        with_token.target = without_token.target = port
        # No token on the client: the proxy supplies it as a header.
        assert (await run_proxyflow(await with_token.start(), None)).endswith("hi")
        assert policy.headers
        assert policy.headers[-1]["x-forwarded-for"] == "203.0.113.7"
        # A proxy that forwards the address but not the token is refused.
        assert await refused(await without_token.start(), None)
    finally:
        await with_token.close()
        await without_token.close()
        await serving.close()


async def test_flowmaxsize_default_matches_the_client(tmp_path: Path) -> None:
    args = _parse_args(["--config", "x.toml"])
    client_default = inspect.signature(WebSocketClientTransport.connect).parameters["max_size"]
    assert args.max_size == 16 * 1024 * 1024 == client_default.default
    host = build_host(load_config(write_config(tmp_path)), store=False)
    serving = Serving(host, "127.0.0.1", TOKEN, max_size=args.max_size)
    try:
        port = await serving.port()
        transport = await WebSocketClientTransport.connect(f"ws://127.0.0.1:{port}/", token=TOKEN)
        try:
            await transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "channel": ROOT_URI,
                        "clientId": "c1",
                        "protocolVersions": ["1.0.0", "0.9.0"],
                        "_meta": {"pad": "x" * (2 * 2**20)},
                    },
                }
            )
            reply = await asyncio.wait_for(transport.receive(), timeout=5)
        finally:
            await transport.close()
        assert reply is not None
        assert "result" in reply
    finally:
        await serving.close()
