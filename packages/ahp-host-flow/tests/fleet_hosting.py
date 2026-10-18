"""A whole fleet in one process: a gateway over memory transports, two worker nodes and a flow node.

Not a test module. The nodes are stock hosts (the worker providers are echo providers whose
sessions can be held), the flow node runs the real `FlowProvider`, and the surface is the real
client, so a run is ordinary AHP end to end.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ahp_client import Client, TurnCompleted, TurnFailed, connect
from ahp_gateway.core import Gateway, OrchestratorConfig
from ahp_gateway.registry import NodeRecord, Principal, StaticInventory
from ahp_host import ConnectionInfo, Host, LoopbackSingleUserPolicy
from ahp_host.core.store import SessionStore
from ahp_host.provider.base import AgentSessionContext, TurnSink, UserMessage
from ahp_host.provider.echo import EchoProvider, EchoSession
from ahp_protocol import Transport, memory_pair

from ahp_host_flow import FlowProvider, load_config
from tests.flow_hosting import store_for, write_toml

DEV = frozenset({"dev"})
TOML = """
[providers.alpha]
via = "fleet"
node = "alpha"
agent = "claude"

[providers.beta]
via = "fleet"
node = "beta"
agent = "claude"

[providers.vault]
via = "fleet"
node = "vault"
agent = "claude"

[routing]
default = "alpha"
rules = [
  { provider = "beta", label = "B*" },
  { provider = "vault", label = "V*" },
]
"""


class HostConnector:
    """Dials in-process hosts over memory pairs; `hosts` may change, and a link may be severed."""

    def __init__(self, hosts: Mapping[str, Host]) -> None:
        self.hosts = dict(hosts)
        self.tasks: list[asyncio.Task[None]] = []
        self.links: dict[str, list[Transport]] = {}

    async def connect(self, record: NodeRecord, principal: Principal) -> Transport:
        host = self.hosts.get(record.node_id)
        if host is None:
            raise OSError(f"{record.node_id} is unreachable")
        client_end, server_end = memory_pair()
        self.links.setdefault(record.node_id, []).append(server_end)
        self.tasks.append(asyncio.create_task(host.serve(server_end)))
        return client_end

    async def sever(self, node_id: str) -> None:
        for transport in self.links.pop(node_id, []):
            await transport.close()


class GatedEchoSession(EchoSession):
    def __init__(self, context: AgentSessionContext, gate: asyncio.Event, **options: Any) -> None:
        super().__init__(context, **options)
        self._gate = gate

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await self._gate.wait()
        await super().send_user_message(message, sink)


class GatedEchoProvider(EchoProvider):
    """Echo whose sessions wait for `gate` before replying; set by default, a test clears it."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.gate = asyncio.Event()
        self.gate.set()

    async def create_session(self, context: AgentSessionContext) -> GatedEchoSession:
        return GatedEchoSession(context, self.gate, structured=self._structured)


@dataclass
class FleetFixture:
    base: Path
    hosts: dict[str, Host]
    connector: HostConnector
    gateway: Gateway
    serving: list[asyncio.Task[None]]
    providers: dict[str, Any]
    make_gateway: Callable[[], Gateway]
    make_flow: Callable[[], Host]

    @property
    def alpha(self) -> GatedEchoProvider:
        return self._gated("alpha")

    @property
    def beta(self) -> GatedEchoProvider:
        return self._gated("beta")

    def _gated(self, node: str) -> GatedEchoProvider:
        provider = self.providers[node]
        assert isinstance(provider, GatedEchoProvider)
        return provider

    @contextlib.asynccontextmanager
    async def surface(self, client_id: str = "surface-1") -> AsyncIterator[Client]:
        client_end, gateway_end = memory_pair()
        self.serving.append(asyncio.create_task(self.gateway.serve(gateway_end)))
        async with connect(transport=client_end, reconnect=False, client_id=client_id) as client:
            yield client

    async def aclose(self) -> None:
        await self.gateway.aclose()
        for task in [*self.serving, *self.connector.tasks]:
            task.cancel()
        for task in [*self.serving, *self.connector.tasks]:
            with contextlib.suppress(BaseException):
                await task
        for host in self.hosts.values():
            await host.aclose()


async def boot_fleet(
    base: Path,
    *,
    flow_toml_body: str = TOML,
    gateway_options: Mapping[str, Any] | None = None,
    flow_store: SessionStore | None = None,
    node_providers: Mapping[str, Any] | None = None,
) -> FleetFixture:
    """`alpha`, `beta`: gated echo; `vault`: another group's; `flow`: the flow agent."""
    (base / "user").mkdir(parents=True, exist_ok=True)
    providers: dict[str, Any] = {
        "alpha": GatedEchoProvider(provider_id="claude", structured=True),
        "beta": GatedEchoProvider(provider_id="claude", structured=True),
        "vault": EchoProvider(provider_id="claude"),
        **(node_providers or {}),
    }
    hosts = {name: Host(p, LoopbackSingleUserPolicy()) for name, p in providers.items()}
    config = load_config(write_toml(base, "flow", flow_toml_body))
    flow = FlowProvider(config, user_workflows_dir=base / "user", fleet_client_wait=5.0)
    hosts["flow"] = Host(flow, LoopbackSingleUserPolicy(), store=flow_store)
    records = [
        NodeRecord(name, f"mem://{name}", frozenset({"ops"}) if name == "vault" else DEV)
        for name in hosts
    ]
    connector = HostConnector(hosts)

    def authenticate(info: ConnectionInfo) -> Principal:
        return Principal(info.client_id, DEV)

    def make_gateway() -> Gateway:
        return Gateway(
            StaticInventory(records),
            connector,
            authenticate,
            redial_backoff=(0.01, 0.05),
            orchestrator=OrchestratorConfig(
                child_config={}, state_path=base / "gateway.json", **(gateway_options or {})
            ),
        )

    def make_flow() -> Host:
        """A flow host as a restart builds it: same state dir and store, a fresh provider."""
        again = load_config(write_toml(base, "flow", flow_toml_body))
        provider = FlowProvider(again, user_workflows_dir=base / "user", fleet_client_wait=5.0)
        return Host(provider, LoopbackSingleUserPolicy(), store=store_for(base))

    return FleetFixture(
        base, hosts, connector, make_gateway(), [], providers, make_gateway, make_flow
    )


async def restart_flow_host(fx: FleetFixture, *, drain: float | None = 0.2) -> Host:
    """Replace the flow host with a restored one over the same store; the gateway redials it.

    With *drain* (the default) it is a deliberate restart whose drain deadline passed: the
    restored host resumes the cut-off run by itself. With None it stands for a crash: the
    run waits, failed and resumable, for a client's `chat/turnResume`. Either way the old
    host is closed as a stopping process closes it, its connections included; the run's
    node sessions are left running (only an abort stops them).

    The node is unreachable until the new host is in place, as a restarting process is, so
    the gateway's supervised link cannot land on the old host again.
    """
    old = fx.connector.hosts.pop("flow")
    if drain is not None:
        await old.drain(drain)
    await old.aclose()
    await fx.connector.sever("flow")
    host = fx.make_flow()
    await host.restore()
    fx.hosts["flow"] = host
    fx.connector.hosts["flow"] = host
    return host


async def restart_gateway(fx: FleetFixture) -> Gateway:
    """Close the gateway and start a new one on the same connector, records and state file."""
    await fx.gateway.aclose()
    for task in fx.serving:
        task.cancel()
    for task in fx.serving:
        with contextlib.suppress(BaseException):
            await task
    fx.serving.clear()
    fx.gateway = fx.make_gateway()
    await fx.gateway.start()
    return fx.gateway


async def start_run(surface: Client, uri: str, text: str) -> TurnCompleted | TurnFailed:
    """Create flow session *uri* through the gateway, prompt its default chat, return the end."""
    session = await surface.create_session(provider="flow", uri=uri)
    chat = await session.chat()
    async with asyncio.timeout(10):
        end = await chat.prompt(text)
    assert isinstance(end, TurnCompleted | TurnFailed), end
    return end


def final_json_of(end: TurnCompleted) -> Any:
    """The JSON block a workflow run ends its turn with."""
    start = end.text.index("```json\n") + len("```json\n")
    return json.loads(end.text[start : end.text.rindex("\n```")])


def chat_state(host: Host, session: str) -> dict[str, Any]:
    """The default chat's state of *session* on *host*."""
    found = host.sequencer.state_of(session)
    assert isinstance(found, Mapping), session
    chat = host.sequencer.state_of(str(found["defaultChat"]))
    return dict(chat) if isinstance(chat, Mapping) else {}


def session_uris(host: Host, scheme: str) -> list[str]:
    """The sessions *host* holds, by URI scheme (the sequencer has no public enumeration).

    A session's sub-resources (`<session>/annotations`) share its scheme; only the session
    itself, whose state carries a `defaultChat`, counts.
    """
    return sorted(
        uri
        for uri, state in host.sequencer._states.items()
        if uri.startswith(f"{scheme}:/") and isinstance(state, Mapping) and "defaultChat" in state
    )


def messages_of(host: Host, session: str) -> list[str]:
    """The text of every turn message in *session*'s default chat, running turn included."""
    state = chat_state(host, session)
    turns = [*state.get("turns", []), *([state["activeTurn"]] if state.get("activeTurn") else [])]
    return [str(turn["message"]["text"]) for turn in turns]


def active_count(host: Host, scheme: str) -> int:
    """How many of *host*'s sessions have a turn running now."""
    return sum(1 for uri in session_uris(host, scheme) if chat_state(host, uri).get("activeTurn"))
