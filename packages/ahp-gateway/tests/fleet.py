"""A fleet in one process: real sibling hosts as nodes, the real client as a surface.

Nothing here is a fake AHP peer. The nodes are `agent_host_server.Host` with
its echo provider, and the surface is `agent_host_client.connect`, so a test
that passes is evidence that stock peers on both edges accept the broker -
which is what docs/plan.md §8 says a milestone must prove.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from agent_host_client import Client, connect
from agent_host_protocol import Transport, memory_pair
from agent_host_server import ConnectionInfo, Host, LoopbackSingleUserPolicy
from agent_host_server.provider.echo import EchoProvider

from agent_host_broker.core import Broker
from agent_host_broker.registry import NodeRecord, Principal, StaticInventory

DEV = frozenset({"dev"})


class HostConnector:
    """Dials in-process hosts over memory pairs. Unknown ids are unreachable."""

    def __init__(self, hosts: Mapping[str, Host]) -> None:
        self.hosts = dict(hosts)
        self.tasks: list[asyncio.Task[None]] = []
        self.dialed: list[tuple[str, str]] = []

    async def connect(self, record: NodeRecord, principal: Principal) -> Transport:
        self.dialed.append((record.node_id, principal.subject))
        host = self.hosts.get(record.node_id)
        if host is None:
            raise OSError(f"{record.node_id} is unreachable")
        client_end, server_end = memory_pair()
        self.tasks.append(asyncio.create_task(host.serve(server_end)))
        return client_end


def echo_host(provider_id: str) -> Host:
    return Host(EchoProvider(provider_id=provider_id), LoopbackSingleUserPolicy())


class Fleet:
    def __init__(
        self,
        hosts: Mapping[str, Host],
        records: list[NodeRecord],
        authenticate: Callable[[ConnectionInfo], Principal | None],
    ) -> None:
        self.hosts = dict(hosts)
        self.connector = HostConnector(hosts)
        self.broker = Broker(StaticInventory(records), self.connector, authenticate)
        self._serving: list[asyncio.Task[None]] = []

    def surface_transport(self) -> Transport:
        client_end, broker_end = memory_pair()
        self._serving.append(asyncio.create_task(self.broker.serve(broker_end)))
        return client_end

    @contextlib.asynccontextmanager
    async def surface(self, client_id: str = "surface-1") -> AsyncIterator[Client]:
        async with connect(
            transport=self.surface_transport(), reconnect=False, client_id=client_id
        ) as client:
            yield client

    @contextlib.asynccontextmanager
    async def direct(self, node_id: str) -> AsyncIterator[Client]:
        """A stock client on a bare node, with no broker in the path."""
        client_end, server_end = memory_pair()
        task = asyncio.create_task(self.hosts[node_id].serve(server_end))
        async with connect(transport=client_end, reconnect=False, client_id="direct") as client:
            yield client
        await client_end.close()
        await task

    async def aclose(self) -> None:
        for task in [*self._serving, *self.connector.tasks]:
            task.cancel()
        for task in [*self._serving, *self.connector.tasks]:
            with contextlib.suppress(BaseException):
                await task
        for host in self.hosts.values():
            await host.aclose()


def everyone_is_a_dev(info: ConnectionInfo) -> Principal:
    return Principal(info.client_id, DEV)


def providers(client: Client) -> set[Any]:
    return {agent.get("provider") for agent in client.agents()}
