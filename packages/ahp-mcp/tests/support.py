"""A fleet in one process, and the MCP server in front of it.

Nothing is a fake of AHP: the machines are stock `ahp_host.Host`s with the echo
agent, serving real directories, behind a real `ahp_gateway.Gateway`. The MCP
side is the SDK's own in-process client. A passing test is evidence that the
tools work against the peers they will meet.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from ahp_client import connect
from ahp_client.api import ClientContext
from ahp_gateway.core import Gateway
from ahp_gateway.registry import NodeRecord, Principal, StaticInventory
from ahp_host import ConnectionInfo, Host, LoopbackSingleUserPolicy
from ahp_host.core.resources import RootedFilesystemResourceProvider
from ahp_host.provider.echo import EchoProvider
from ahp_protocol import Transport, memory_pair
from mcp import Client as McpClient

from ahp_mcp import Fleet, build_server

DEV = frozenset({"dev"})


def rooted_host(root: Path, *, delay: float = 0.0) -> Host:
    return Host(
        EchoProvider(provider_id="claude", delay=delay),
        LoopbackSingleUserPolicy(),
        resources=RootedFilesystemResourceProvider(root),
        default_directory=root.resolve().as_uri(),
    )


def tree(base: Path, name: str, files: list[str]) -> Path:
    root = base / name / "src"
    root.mkdir(parents=True)
    for file in files:
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        (root / file).write_text(file)
    return root


class _Connector:
    def __init__(self, hosts: Mapping[str, Host]) -> None:
        self.hosts = dict(hosts)
        self.tasks: list[asyncio.Task[None]] = []

    async def connect(self, record: NodeRecord, principal: Principal) -> Transport:
        client_end, server_end = memory_pair()
        self.tasks.append(asyncio.create_task(self.hosts[record.node_id].serve(server_end)))
        return client_end


def _everyone(info: ConnectionInfo) -> Principal:
    return Principal(info.client_id, DEV)


class World:
    """Two machines, `node-a` and `node-b`, behind a gateway."""

    def __init__(self, tmp: Path, *, delay: float = 0.0) -> None:
        self.roots = {
            "node-a": tree(tmp, "a", ["app/main.py", "notes.txt"]),
            "node-b": tree(tmp, "b", ["game/play.py"]),
        }
        self.hosts = {name: rooted_host(root, delay=delay) for name, root in self.roots.items()}
        self.connector = _Connector(self.hosts)
        self.gateway = Gateway(
            StaticInventory([NodeRecord(n, f"mem://{n}", DEV) for n in self.hosts]),
            self.connector,
            _everyone,
        )
        self._serving: list[asyncio.Task[None]] = []

    def connect_gateway(self) -> ClientContext:
        async def dial() -> Transport:
            client_end, gateway_end = memory_pair()
            self._serving.append(asyncio.create_task(self.gateway.serve(gateway_end)))
            return client_end

        return connect(transport_factory=dial, client_id="ahp-mcp-test", reconnect=False)

    def connect_host(self, name: str) -> ClientContext:
        async def dial() -> Transport:
            client_end, host_end = memory_pair()
            self._serving.append(asyncio.create_task(self.hosts[name].serve(host_end)))
            return client_end

        return connect(transport_factory=dial, client_id="ahp-mcp-test", reconnect=False)

    async def aclose(self) -> None:
        await self.gateway.aclose()
        for task in [*self._serving, *self.connector.tasks]:
            task.cancel()
        for task in [*self._serving, *self.connector.tasks]:
            with contextlib.suppress(BaseException):
                await task
        for host in self.hosts.values():
            await host.aclose()


@contextlib.asynccontextmanager
async def mcp_client(fleet: Fleet) -> AsyncIterator[McpClient]:
    try:
        async with McpClient(build_server(fleet)) as client:
            yield client
    finally:
        await fleet.aclose()


def text(result: Any) -> str:
    return "\n".join(getattr(part, "text", "") for part in result.content)
