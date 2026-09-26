"""A fleet in one process: real sibling hosts as nodes, the real client as a surface.

Nothing here is a fake AHP peer. The nodes are `agent_host_server.Host` with
its echo provider, and the surface is `agent_host_client.connect`, so a test
that passes is evidence that stock peers on both edges accept the broker -
which is what docs/plan.md §8 says a milestone must prove.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from agent_host_client import Client, connect
from agent_host_client.hosts import immediate_forever_policy
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
        #: Per node, the host-side end of every link dialed, newest last.
        self.links: dict[str, list[Transport]] = {}

    async def connect(self, record: NodeRecord, principal: Principal) -> Transport:
        self.dialed.append((record.node_id, principal.subject))
        host = self.hosts.get(record.node_id)
        if host is None:
            raise OSError(f"{record.node_id} is unreachable")
        client_end, server_end = memory_pair()
        self.links.setdefault(record.node_id, []).append(server_end)
        self.tasks.append(asyncio.create_task(host.serve(server_end)))
        return client_end

    async def sever(self, node_id: str) -> None:
        """Cut every live link to a node, as a network drop would."""
        for transport in self.links.pop(node_id, []):
            await transport.close()


def echo_host(provider_id: str) -> Host:
    return Host(EchoProvider(provider_id=provider_id), LoopbackSingleUserPolicy())


class Fleet:
    def __init__(
        self,
        hosts: Mapping[str, Host],
        records: list[NodeRecord],
        authenticate: Callable[[ConnectionInfo], Principal | None],
        **broker_options: Any,
    ) -> None:
        self.hosts = dict(hosts)
        self.connector = HostConnector(hosts)
        broker_options.setdefault("redial_backoff", (0.01, 0.05))
        self.broker = Broker(
            StaticInventory(records), self.connector, authenticate, **broker_options
        )
        self._serving: list[asyncio.Task[None]] = []
        #: The broker-side end of every surface connection, newest last.
        self.surface_ends: list[Transport] = []

    def surface_transport(self) -> Transport:
        client_end, broker_end = memory_pair()
        self.surface_ends.append(broker_end)
        self._serving.append(asyncio.create_task(self.broker.serve(broker_end)))
        return client_end

    @contextlib.asynccontextmanager
    async def surface(self, client_id: str = "surface-1") -> AsyncIterator[Client]:
        async with connect(
            transport=self.surface_transport(), reconnect=False, client_id=client_id
        ) as client:
            yield client

    @contextlib.asynccontextmanager
    async def supervised_surface(self, client_id: str = "surface-1") -> AsyncIterator[Client]:
        """A stock client that reconnects, as a real app's would."""

        async def dial() -> Transport:
            return self.surface_transport()

        async with connect(
            transport_factory=dial,
            client_id=client_id,
            reconnect_policy=immediate_forever_policy(),
        ) as client:
            yield client

    async def drop_surface(self) -> None:
        await self.surface_ends[-1].close()

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


async def reconnected(client: Client, past: int, timeout: float = 5.0) -> None:
    """Wait until the client's supervisor has completed a connect after `past`."""
    runtime: Any = client._runtime  # the supervisor; no public accessor exists
    deadline = time.monotonic() + timeout
    while not (runtime.generation > past and runtime.state.status == "connected"):
        if time.monotonic() > deadline:
            raise TimeoutError(f"no reconnect past generation {past}: {runtime.state}")
        await asyncio.sleep(0.01)


def generation(client: Client) -> int:
    runtime: Any = client._runtime
    return int(runtime.generation)


def everyone_is_a_dev(info: ConnectionInfo) -> Principal:
    return Principal(info.client_id, DEV)


def providers(client: Client) -> set[Any]:
    return {agent.get("provider") for agent in client.agents()}
