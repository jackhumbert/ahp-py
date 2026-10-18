"""A fleet for the flow tools: a flow node, workers, and one node a dev may not use.

Not a test module. The nodes are stock hosts with the echo provider, so every
call a flow session makes is ordinary AHP a real node accepts.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ahp_client import Client, TurnCompleted
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.provider.base import (
    AgentSessionContext,
    ClientToolCall,
    ConfigRequest,
    ConfigResolution,
    TurnSink,
    UserMessage,
)
from ahp_host.provider.echo import EchoProvider, EchoSession

from ahp_gateway.core import OrchestratorConfig
from ahp_gateway.registry import NodeRecord
from tests.fleet import DEV, Fleet, everyone_is_a_dev


class RecordingEcho(EchoProvider):
    """Echo that offers the two settings a flow call may set, and notes what it got."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.configs: list[dict[str, Any]] = []

    async def resolve_config(self, request: ConfigRequest) -> ConfigResolution:
        return ConfigResolution(
            properties={
                "permissionMode": {"type": "string", "title": "Approvals"},
                "effort": {"type": "string", "title": "Effort"},
            },
            values={k: v for k, v in request.values.items() if k in ("permissionMode", "effort")},
        )

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        self.configs.append(dict(context.config))
        return await super().create_session(context)


class ToolCallerSession(EchoSession):
    """A flow session's agent that runs one fleet tool per message, as a workflow engine does.

    A message that is a JSON object `{"tool": name, "args": {...}}` becomes a
    client tool call to the active client offering *name* (the gateway), made
    the way ahp-host-flow makes it: through the turn's `run_client_tool`, so
    the call goes over the wire to the gateway and its answer comes back the
    same way. The reply is that answer as JSON. Anything else is echoed.
    """

    def __init__(self, context: AgentSessionContext, **options: Any) -> None:
        super().__init__(context, **options)
        # The creator is in the context; clients that join later arrive through
        # `active_clients_changed`.
        self.clients: list[Mapping[str, Any]] = (
            [{"clientId": context.active_client_id, "tools": list(context.client_tools)}]
            if context.active_client_id
            else []
        )

    async def active_clients_changed(self, clients: Sequence[Mapping[str, Any]]) -> None:
        self.clients = [dict(c) for c in clients]

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        try:
            request = json.loads(message.text)
        except ValueError:
            request = None
        if not isinstance(request, dict) or not isinstance(request.get("tool"), str):
            await super().send_user_message(message, sink)
            return
        name = request["tool"]
        owner = next(
            (
                c["clientId"]
                for c in self.clients
                if any(t.get("name") == name for t in c.get("tools") or [])
            ),
            None,
        )
        if owner is None:
            await sink.text_delta(json.dumps({"error": f"no active client offers {name}"}))
            return
        result = await sink.run_client_tool(
            ClientToolCall(
                call_id=f"call-{uuid.uuid4()}",
                name=name,
                client_id=str(owner),
                tool_input=dict(request.get("args") or {}),
                display_name=name,
            )
        )
        await sink.text_delta(
            json.dumps(
                {"accepted": result.accepted, "reason": result.reason, "value": result.value}
            )
        )


class ToolCaller(EchoProvider):
    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        return ToolCallerSession(context)

    async def resume_session(self, context: AgentSessionContext) -> EchoSession:
        return ToolCallerSession(context)


async def call_through_the_node(
    surface: Client, uri: str, tool: str, **arguments: Any
) -> dict[str, Any]:
    """Have flow session *uri*'s agent call *tool* through the gateway; return its result.

    End to end: the surface prompts the flow session through the gateway, the
    node's agent makes a real client tool call, the gateway's tool host runs it
    and answers over its own link, and the agent replies with the answer.
    """
    session = await surface.open_session(uri)
    chat = await session.chat()
    async with asyncio.timeout(15):
        end = await chat.prompt(json.dumps({"tool": tool, "args": arguments}))
    assert isinstance(end, TurnCompleted), end
    reply: dict[str, Any] = json.loads(end.text)
    assert "accepted" in reply, reply
    if not reply["accepted"]:
        # A tool's failure reaches the node as a refused call, with its message.
        return {"success": False, "reason": reply.get("reason")}
    value: dict[str, Any] = reply["value"]
    return value


def _host(provider: EchoProvider) -> Host:
    return Host(provider, LoopbackSingleUserPolicy())


def flow_fleet(tmp_path: Path, **config: Any) -> Fleet:
    """`flow` runs the flow agent; `alpha`/`beta` and `asker` run `claude`.

    `asker`'s turns wait on input, so its sessions stay active. `vault` is
    admitted to the `ops` group only, so a dev principal may not use it. `hosts`
    reuses a previous fleet's nodes, as a gateway restart does.
    """
    hosts = config.pop("hosts", None) or {
        "flow": _host(ToolCaller(provider_id="flow")),
        "alpha": _host(RecordingEcho(provider_id="claude", structured=True)),
        "beta": _host(RecordingEcho(provider_id="claude", structured=True)),
        "asker": _host(EchoProvider(provider_id="claude", elicit=True)),
        "vault": _host(EchoProvider(provider_id="claude")),
    }
    records = [
        NodeRecord(name, f"mem://{name}", frozenset({"ops"}) if name == "vault" else DEV)
        for name in hosts
    ]
    child_config = config.pop("child_config", {})
    return Fleet(
        hosts,
        records,
        everyone_is_a_dev,
        orchestrator=OrchestratorConfig(child_config=child_config, **config),
    )


async def run_tool(fleet: Fleet, uri: str, tool: str, **arguments: Any) -> dict[str, Any]:
    """Run one flow tool as the flow node would ask for it, and return its result."""
    orchestrator = fleet.gateway.orchestrator
    assert orchestrator is not None
    adopted = orchestrator._adopted[uri]
    assert adopted.tools is not None
    _, executor = adopted.tools._tools[tool]
    assert executor is not None
    result: dict[str, Any] = await executor(
        {"turnId": "t1", "toolCallId": f"call-{tool}", "toolInput": json.dumps(arguments)}
    )
    return result


async def start_flow(fleet: Fleet, uri: str = "flow:/r1", client_id: str = "surface-1") -> None:
    async with fleet.surface(client_id) as surface:
        await surface.protocol.create_session(uri, provider="flow")


async def eventually(condition: Callable[[], Any], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def active_clients(host: Host, uri: str) -> list[Mapping[str, Any]]:
    state = host.sequencer.state_of(uri)
    clients = state.get("activeClients") if isinstance(state, Mapping) else None
    return list(clients or [])
