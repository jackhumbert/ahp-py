"""An agent that runs sessions on the fleet's machines: fleet tools for one session.

A surface starts one like any session, by picking the **orchestrator** agent
the gateway adds to the merged agent list. The session itself is an ordinary
session of a real agent on a real node (`OrchestratorConfig.provider`,
normally Claude). What makes it an orchestrator is that the gateway joins it as
an active client offering *fleet tools*: list the machines, start a session on
one, message it, read it, wait for it, stop it. The agent calls them like any
tool. They run here, in the gateway, over links of its own.

**Why links of its own.** The surface's links belong to its connection and
close with it. An orchestrator keeps working after the phone that started it
goes to sleep, and the calls have to be answered by a client that is still
there. So the orchestrator holds one supervised `ahp_client` connection per
(principal, node). It opens them with the connector and credentials a surface
connection uses, and only to nodes the registry admits that principal to
(invariant 2). To every node it is a client like any other (invariant 3), with
an id of its own.

**What it may not do.** These limits hold whatever the agent is told:

* A session it starts runs with `OrchestratorConfig.child_config` (by default
  `permissionMode: auto`). The agent cannot choose a looser mode, so the
  node's own approval policy (Claude Code's classifier, in auto) gates every
  action the worker takes.
* It never answers an approval. There is no tool for it: a prompt a worker
  raises waits for a person, in any client, like any other session's.
* It can only message, read or stop sessions it started itself, not the
  user's other sessions.
* At most `max_running` of its sessions run a turn at once.
* Workers get no fleet tools, so a worker cannot start more workers.

Which orchestrators exist, and which sessions each started, is kept in
`OrchestratorConfig.state_path`. After a restart, each orchestrator is joined
again (`Client.open_session(tools=...)`) so its tools keep working.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from ahp_client import Client, Session, connect
from ahp_client.client import actions
from ahp_client.serve import ClientToolHost
from ahp_protocol import AhpError, Transport
from ahp_protocol.errors import invalid_params
from ahp_protocol.ext.delegation import delegation_origin
from ahp_protocol.ext.structured import (
    OUTPUT_SCHEMA_META_KEY,
    STRUCTURED_OUTPUT_TOOL,
    STRUCTURED_OUTPUT_TOOL_META_KEY,
    unwrap,
)

from ahp_gateway.registry import NodeRecord, Principal

if TYPE_CHECKING:
    from ahp_gateway.core.gateway import Gateway

__all__ = ["FLEET_TOOLS", "FLOW_TOOLS", "Orchestrator", "OrchestratorConfig"]

_log = logging.getLogger(__name__)

#: `_meta` key on a spawned session's summary and on each fleet tool call's
#: result, naming the orchestrator session that started it.
SPAWNED_BY_META_KEY: Final = "ahp-gateway/spawnedBy"

#: Permission modes, strictest first (Claude Code's names, the ones a workflow
#: engine sends). A flow session may ask for a mode no looser than `child_config`'s.
PERMISSION_ORDER: Final = ("plan", "default", "acceptEdits", "auto", "bypassPermissions")

#: Effort levels a flow session may name; the same set a workflow's config takes.
EFFORT_LEVELS: Final = ("low", "medium", "high", "xhigh", "max")

#: Seconds `max_running` waits to open a flow child it has not seen finished.
FLOW_OPEN_TIMEOUT: Final = 5.0

#: Seconds between looks at the mirror while a flow call waits. In-memory turns
#: settle in milliseconds, and 50 ms keeps a 600 s wait to 12000 reads at most.
FLOW_POLL: Final = 0.05


@dataclass(frozen=True)
class OrchestratorConfig:
    """Turns the orchestrator on. Nothing here is advertised without it."""

    #: The agent id surfaces pick to start one. Must not be a real provider's.
    agent: str = "orchestrator"
    display_name: str = "Orchestrator"
    description: str = "Plans work and runs it as sessions on your machines."
    #: The real agent an orchestrator session runs on, and the default for the
    #: sessions it starts.
    provider: str = "claude"
    #: Where orchestrator sessions run, when the request names no folder. None:
    #: the first node, in inventory order, offering `provider`.
    node: str | None = None
    #: Merged over whatever config a worker's agent resolves, and not
    #: changeable by the orchestrator. The default asks Claude Code's auto mode.
    child_config: Mapping[str, Any] = field(default_factory=lambda: {"permissionMode": "auto"})
    #: How many of one orchestrator's sessions may run a turn at the same time.
    max_running: int = 4
    #: JSON file holding which orchestrators exist and what each started. None:
    #: in memory only, so a restart forgets them.
    state_path: Path | None = None
    #: Agent ids whose sessions get the engine-facing flow tools, not the
    #: orchestrator's. Their sessions are created as asked, not rewritten.
    flow_agents: tuple[str, ...] = ("flow",)
    #: Seconds a flow `start_session` waits for a free slot under `max_running`.
    slot_wait: float = 600.0

    def __post_init__(self) -> None:
        if self.agent in self.flow_agents:
            raise ValueError(f"{self.agent!r} cannot be both the orchestrator and a flow agent")


#: The tools, as `ToolDefinition`s. Descriptions are the agent's only manual.
FLEET_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "list_machines",
        "title": "List machines",
        "description": (
            "List the machines you can start sessions on: each one's id, name, whether "
            "it is online, the agents it runs, and its top-level folders."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_folder",
        "title": "List folder",
        "description": "List what is in a folder on a machine.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "machine": {"type": "string", "description": "A machine id from list_machines."},
                "folder": {
                    "type": "string",
                    "description": "A folder path or file URI on that machine.",
                },
            },
            "required": ["machine", "folder"],
        },
    },
    {
        "name": "start_session",
        "title": "Start session",
        "description": (
            "Start a coding session on a machine and send it its first message. It runs on "
            "its own, in auto mode: its actions are checked by the machine's safety "
            "classifier, and anything the classifier will not decide waits for the user. "
            "Returns the session id to use with the other tools."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "machine": {"type": "string", "description": "A machine id from list_machines."},
                "prompt": {"type": "string", "description": "The task, in full."},
                "folder": {
                    "type": "string",
                    "description": "The folder to work in (path or file URI on that machine).",
                },
                "agent": {"type": "string", "description": "The agent to use, if not the default."},
                "title": {"type": "string", "description": "A short name for the session."},
            },
            "required": ["machine", "prompt"],
        },
    },
    {
        "name": "send_message",
        "title": "Message session",
        "description": "Send another message to a session you started. It must not be busy.",
        "inputSchema": {
            "type": "object",
            "properties": {"session": {"type": "string"}, "prompt": {"type": "string"}},
            "required": ["session", "prompt"],
        },
    },
    {
        "name": "read_session",
        "title": "Read session",
        "description": (
            "Read a session you started: its status, whatever it is waiting on, and its "
            "last turns (the messages, its answers, and a line per tool it used)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "turns": {"type": "integer", "description": "How many recent turns (default 2)."},
            },
            "required": ["session"],
        },
    },
    {
        "name": "wait_for_sessions",
        "title": "Wait for sessions",
        "description": (
            "Wait until sessions you started stop working: finished, failed, or waiting on "
            "the user. Returns each one's status. Waits for all of them, or with "
            "any=true for the first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sessions": {"type": "array", "items": {"type": "string"}},
                "any": {"type": "boolean"},
                "timeout_seconds": {"type": "number", "description": "Default 600."},
            },
            "required": ["sessions"],
        },
    },
    {
        "name": "stop_session",
        "title": "Stop session",
        "description": "Stop the turn a session you started is running.",
        "inputSchema": {
            "type": "object",
            "properties": {"session": {"type": "string"}},
            "required": ["session"],
        },
    },
    {
        "name": "list_sessions",
        "title": "List sessions",
        "description": "List the sessions you have started, with each one's status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
)


#: The tools a flow session is offered: for a workflow engine, not a model. The
#: engine calls them deterministically, so each takes and returns plain data.
FLOW_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "start_session",
        "title": "Start session",
        "description": (
            "For a workflow engine, not a model: start a session of an agent on a node, as the "
            "principal who started this run, and send it its prompt. Returns the session and "
            "chat ids. Deterministic: it never chooses a node, agent or mode itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string"},
                "agent": {"type": "string"},
                "prompt": {"type": "string"},
                "folder": {"type": "string"},
                "model": {"type": "string"},
                "config": {
                    "type": "object",
                    "properties": {
                        "effort": {"type": "string"},
                        "permissionMode": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "output_schema": {"type": "object"},
                "title": {"type": "string"},
                "originChat": {"type": "string"},
                "originToolCall": {"type": "string"},
            },
            "required": ["node", "agent", "prompt"],
        },
    },
    {
        "name": "wait_for_sessions",
        "title": "Wait for sessions",
        "description": (
            "For a workflow engine, not a model: wait until sessions this run started stop "
            "working. Returns each one's status and whether it is done."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sessions": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number"},
            },
            "required": ["sessions"],
        },
    },
    {
        "name": "read_session",
        "title": "Read session",
        "description": (
            "For a workflow engine, not a model: read a session this run started: its final "
            "message, its structured result if the turn produced one, usage and status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session": {"type": "string"}},
            "required": ["session"],
        },
    },
    {
        "name": "stop_session",
        "title": "Stop session",
        "description": (
            "For a workflow engine, not a model: stop the turn a session this run started is "
            "running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session": {"type": "string"}},
            "required": ["session"],
        },
    },
)


@dataclass
class _Spawned:
    """A session an orchestrator started."""

    uri: str
    node: str
    parent: str
    title: str
    tool_call_id: str | None = None
    #: Its default chat, when known (a flow child's, recorded at start): what a
    #: surface prompts, so another principal can be refused it by URI alone.
    chat: str | None = None
    #: Seen finished (done, stopped, failed or gone). Persisted, so a restarted
    #: gateway counts toward `max_running` only the children that may still run.
    finished: bool = False


@dataclass
class _Adopted:
    """An orchestrator session, and whose it is."""

    uri: str
    node: str
    principal: Principal
    tools: ClientToolHost | None = None
    children: dict[str, _Spawned] = field(default_factory=dict)
    #: "orchestrator" or "flow": which tool set the session is offered.
    kind: str = "orchestrator"
    #: Serialises a flow session's starts, so `max_running` is checked and taken atomically.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ToolError(Exception):
    """A tool call that cannot be done, said to the agent as the tool's failure."""


class Orchestrator:
    def __init__(self, gateway: Gateway, config: OrchestratorConfig) -> None:
        self.gateway = gateway
        self.config = config
        #: One supervised connection per (principal subject, node).
        self._clients: dict[tuple[str, str], Client] = {}
        self._connecting: dict[tuple[str, str], asyncio.Lock] = {}
        self._adopted: dict[str, _Adopted] = {}
        #: Each worker session, subscribed with its default chat, once.
        self._sessions: dict[str, tuple[Client, Session]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # ─── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Rejoin every orchestrator the state file names, in the background.

        In the background because a node may be off: its orchestrators are
        joined when it answers, and the gateway serves meanwhile.
        """
        for adopted in self._load():
            self._adopted[adopted.uri] = adopted
            self._spawn(self._rejoin(adopted))

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            with contextlib.suppress(Exception):
                await client.aclose()

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        task: asyncio.Task[None] = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _rejoin(self, adopted: _Adopted) -> None:
        delay = 1.0
        while True:
            try:
                client = await self._client(adopted.principal, adopted.node)
                adopted.tools = (
                    self._flow_tool_host(client, adopted)
                    if adopted.kind == "flow"
                    else self._tool_host(client, adopted)
                )
                await client.open_session(adopted.uri, tools=adopted.tools)
                return
            except Exception as exc:
                _log.info("orchestrator %s not rejoined yet: %r", adopted.uri, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    # ─── the agent surfaces see ─────────────────────────────────────────

    def agent_entry(self, merged_agents: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        """Its entry in the merged agent list, modelled on the agent it runs on.

        Absent when no admitted node runs that agent: the fleet cannot start
        one then, and advertising it would say otherwise (invariant 4).
        """
        base = next((a for a in merged_agents if a.get("provider") == self.config.provider), None)
        if base is None:
            return None
        entry = dict(base)
        entry.update(
            provider=self.config.agent,
            displayName=self.config.display_name,
            description=self.config.description,
        )
        return entry

    def is_orchestrator(self, provider: Any) -> bool:
        return isinstance(provider, str) and provider == self.config.agent

    def is_flow(self, provider: Any) -> bool:
        return isinstance(provider, str) and provider in self.config.flow_agents

    async def create(self, principal: Principal, node_id: str, params: Mapping[str, Any]) -> None:
        """Start an orchestrator session where the surface asked for one.

        Created by the orchestrator's own connection, on the surface's own
        session URI, so the surface subscribes to it as it would to any session
        it created, and the fleet tools are there from the first turn.
        """
        await self._create_adopted(
            principal, node_id, params, provider=self.config.provider, kind="orchestrator"
        )

    async def create_flow(
        self, principal: Principal, node_id: str, params: Mapping[str, Any]
    ) -> None:
        """Start a flow session the same way, on the agent the surface named.

        The principal is recorded with it: the sessions its tools start are
        started as that principal, whoever later connects.
        """
        await self._create_adopted(
            principal, node_id, params, provider=str(params["provider"]), kind="flow"
        )

    async def _create_adopted(
        self,
        principal: Principal,
        node_id: str,
        params: Mapping[str, Any],
        *,
        provider: str,
        kind: str,
    ) -> None:
        channel = str(params["channel"])
        client = await self._client(principal, node_id)
        adopted = _Adopted(uri=channel, node=node_id, principal=principal, kind=kind)
        adopted.tools = (
            self._flow_tool_host(client, adopted)
            if kind == "flow"
            else self._tool_host(client, adopted)
        )
        directories = params.get("workingDirectories")
        config = params.get("config")
        await client.create_session(
            provider=provider,
            uri=channel,
            working_directories=list(directories) if isinstance(directories, list) else None,
            config=config if isinstance(config, Mapping) else None,
            tools=adopted.tools,
        )
        self._adopted[channel] = adopted
        self._save()

    # ─── connections ─────────────────────────────────────────────────────

    def _client_id(self, principal: Principal) -> str:
        """One id per principal, the same on every node: who these calls are for."""
        digest = hashlib.sha256(principal.subject.encode()).hexdigest()[:12]
        return f"ahp-gateway-orchestrator-{digest}"

    def _record(self, principal: Principal, node_id: str) -> NodeRecord:
        for record in self.gateway.directory.nodes_for(principal):
            if record.node_id == node_id:
                return record
        raise ToolError(f"there is no machine {node_id!r} you may use")

    async def _client(self, principal: Principal, node_id: str) -> Client:
        key = (principal.subject, node_id)
        lock = self._connecting.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self._clients.get(key)
            if existing is not None:
                return existing
            record = self._record(principal, node_id)
            connector = self.gateway.connector

            async def dial() -> Transport:
                return await connector.connect(record, principal)

            client: Client = await connect(
                transport_factory=dial,
                client_id=self._client_id(principal),
                label=f"orchestrator:{node_id}",
            )
            self._clients[key] = client
            return client

    # ─── the tools ───────────────────────────────────────────────────────

    def _tool_host(self, client: Client, adopted: _Adopted) -> ClientToolHost:
        host = ClientToolHost(client.protocol, client_id=client.client_id)
        runners: dict[str, Callable[[_Adopted, dict[str, Any], Mapping[str, Any]], Awaitable[Any]]]
        runners = {
            "list_machines": self._list_machines,
            "list_folder": self._list_folder,
            "start_session": self._start_session,
            "send_message": self._send_message,
            "read_session": self._read_session,
            "wait_for_sessions": self._wait_for_sessions,
            "stop_session": self._stop_session,
            "list_sessions": self._list_sessions,
        }
        for definition in FLEET_TOOLS:
            runner = runners[definition["name"]]
            host.register(definition, self._executor(adopted, runner))
        return host

    def _executor(
        self,
        adopted: _Adopted,
        runner: Callable[[_Adopted, dict[str, Any], Mapping[str, Any]], Awaitable[Any]],
    ) -> Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]:
        async def execute(action: Mapping[str, Any]) -> dict[str, Any]:
            try:
                arguments = _arguments(action.get("toolInput"))
                result = await runner(adopted, arguments, action)
            except (ToolError, AhpError) as exc:
                message = exc.message if isinstance(exc, AhpError) else str(exc)
                return actions.tool_failure_result(message)
            if isinstance(result, Mapping) and "content" in result:
                return {"success": True, **result}
            text = result if isinstance(result, str) else json.dumps(result, indent=2)
            return {"success": True, "content": [{"type": "text", "text": text}]}

        return execute

    async def _list_machines(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        machines: list[dict[str, Any]] = []
        for record in self.gateway.directory.nodes_for(adopted.principal):
            entry: dict[str, Any] = {
                "machine": record.node_id,
                "name": str(record.metadata.get("label") or record.node_id),
            }
            try:
                client = await asyncio.wait_for(
                    self._client(adopted.principal, record.node_id), self.gateway.connect_timeout
                )
            except Exception:
                entry["online"] = False
                machines.append(entry)
                continue
            entry["online"] = True
            entry["agents"] = [str(a.get("provider")) for a in client.agents() if a.get("provider")]
            try:
                entry["folders"] = await self._entries(client, client.default_directory)
            except Exception as exc:
                entry["folders_error"] = str(exc) or type(exc).__name__
            machines.append(entry)
        return machines

    async def _list_folder(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        client = await self._client(adopted.principal, _string(arguments, "machine"))
        return await self._entries(client, _folder_uri(_string(arguments, "folder")))

    async def _entries(self, client: Client, folder: str | None) -> list[dict[str, str]]:
        if folder is None:
            return []
        listed = await client.protocol.resource_list(folder)
        base = folder.rstrip("/")
        return [
            {
                "name": str(e.get("name")),
                "type": str(e.get("type")),
                "uri": f"{base}/{e.get('name')}",
            }
            for e in listed.get("entries") or []
            if isinstance(e, Mapping)
        ]

    async def _start_session(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        running = [c for c in adopted.children.values() if self._is_running(adopted, c)]
        if len(running) >= self.config.max_running:
            raise ToolError(
                f"{len(running)} of your sessions are already running, the most allowed at "
                "once; wait for one to finish"
            )
        node_id = _string(arguments, "machine")
        prompt = _string(arguments, "prompt")
        agent = arguments.get("agent") or self.config.provider
        if not isinstance(agent, str) or self.is_orchestrator(agent):
            raise ToolError("a session you start cannot be another orchestrator")
        folder = arguments.get("folder")
        client = await self._client(adopted.principal, node_id)
        session = await client.create_session(
            provider=agent,
            uri=f"{agent}:/{uuid.uuid4()}",
            working_directories=[_folder_uri(folder)]
            if isinstance(folder, str) and folder
            else None,
            config=dict(self.config.child_config),
        )
        title = arguments.get("title")
        title = title if isinstance(title, str) and title else prompt.splitlines()[0][:60]
        spawned = _Spawned(
            uri=session.uri,
            node=node_id,
            parent=adopted.uri,
            title=title,
            tool_call_id=str(action.get("toolCallId") or "") or None,
        )
        adopted.children[session.uri] = spawned
        self._save()
        chat = await session.chat()
        self._sessions[session.uri] = (client, session)
        client.protocol.dispatch(chat.uri, actions.turn_started(str(uuid.uuid4()), text=prompt))
        with contextlib.suppress(Exception):
            client.protocol.dispatch(session.uri, {"type": "session/titleChanged", "title": title})
        return {
            "content": [
                {
                    "type": "subagent",
                    "resource": chat.uri,
                    "title": title,
                    "agentName": agent,
                    "description": f"on {node_id}",
                },
                {"type": "text", "text": f"Started session {session.uri} on {node_id}."},
            ],
            "pastTenseMessage": f"Started {title} on {node_id}",
        }

    def _child(self, adopted: _Adopted, arguments: Mapping[str, Any]) -> _Spawned:
        uri = _string(arguments, "session")
        child = adopted.children.get(uri)
        if child is None:
            raise ToolError(f"{uri} is not a session you started")
        return child

    async def _worker(self, adopted: _Adopted, child: _Spawned) -> tuple[Client, Session]:
        """The worker's session, subscribed with its default chat."""
        opened = self._sessions.get(child.uri)
        if opened is None:
            client = await self._client(adopted.principal, child.node)
            session = await client.open_session(child.uri)
            await session.chat()
            opened = self._sessions[child.uri] = (client, session)
        return opened

    def _chat_state(self, client: Client, session_uri: str) -> Mapping[str, Any]:
        session = client.mirror.state(session_uri)
        chat = session.get("defaultChat") if isinstance(session, Mapping) else None
        state = client.mirror.state(chat) if isinstance(chat, str) else None
        return state if isinstance(state, Mapping) else {}

    def _is_running(self, adopted: _Adopted, child: _Spawned) -> bool:
        client = self._clients.get((adopted.principal.subject, child.node))
        if client is None:
            return False
        return self._chat_state(client, child.uri).get("activeTurn") is not None

    def _status(self, client: Client, child: _Spawned) -> dict[str, Any]:
        session = client.mirror.state(child.uri)
        session = session if isinstance(session, Mapping) else {}
        chat = self._chat_state(client, child.uri)
        waiting = [
            str(entry.get("kind"))
            for entry in session.get("inputNeeded") or []
            if isinstance(entry, Mapping)
        ]
        turns = chat.get("turns") or []
        last = turns[-1] if turns and isinstance(turns[-1], Mapping) else {}
        if waiting:
            status = "waiting on the user"
        elif chat.get("activeTurn") is not None:
            status = "working"
        elif session.get("lifecycle") == "creationFailed":
            status = "failed to start"
        else:
            status = {"complete": "done", "cancelled": "stopped", "error": "failed"}.get(
                str(last.get("state")), "idle"
            )
        entry: dict[str, Any] = {
            "session": child.uri,
            "title": child.title,
            "machine": child.node,
            "status": status,
        }
        if waiting:
            entry["waiting_on"] = waiting
        return entry

    async def _send_message(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        child = self._child(adopted, arguments)
        prompt = _string(arguments, "prompt")
        client, session = await self._worker(adopted, child)
        if self._chat_state(client, child.uri).get("activeTurn") is not None:
            raise ToolError("that session is busy; wait for it, or stop it first")
        chat = await session.chat()
        client.protocol.dispatch(chat.uri, actions.turn_started(str(uuid.uuid4()), text=prompt))
        return f"Sent to {child.title}."

    async def _read_session(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        child = self._child(adopted, arguments)
        client, _ = await self._worker(adopted, child)
        count = arguments.get("turns")
        count = count if isinstance(count, int) and count > 0 else 2
        chat = self._chat_state(client, child.uri)
        turns = [t for t in chat.get("turns") or [] if isinstance(t, Mapping)]
        active = chat.get("activeTurn")
        if isinstance(active, Mapping):
            turns.append(active)
        turns = turns[-count:]
        status = self._status(client, child)
        lines = [f"{child.title} on {child.node}: {status['status']}"]
        if status.get("waiting_on"):
            lines.append(f"Waiting on the user for: {', '.join(status['waiting_on'])}")
        for turn in turns:
            lines.append("")
            lines.extend(_transcript(turn))
        return "\n".join(lines)

    async def _wait_for_sessions(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        uris = arguments.get("sessions")
        if not isinstance(uris, list) or not uris:
            raise ToolError("name at least one session")
        children = [self._child(adopted, {"session": uri}) for uri in uris]
        clients = {}
        for child in children:
            clients[child.uri], _ = await self._worker(adopted, child)
        timeout = arguments.get("timeout_seconds")
        timeout = float(timeout) if isinstance(timeout, int | float) and timeout > 0 else 600.0
        wanted_any = arguments.get("any") is True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            statuses = [self._status(clients[c.uri], c) for c in children]
            settled = [s["status"] != "working" for s in statuses]
            if (any(settled) if wanted_any else all(settled)) or loop.time() >= deadline:
                return {"timed_out": not any(settled), "sessions": statuses}
            await asyncio.sleep(0.5)

    async def _stop_session(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        child = self._child(adopted, arguments)
        _, session = await self._worker(adopted, child)
        chat = await session.chat()
        await chat.cancel()
        return f"Stopped {child.title}."

    async def _list_sessions(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        listed = []
        for child in adopted.children.values():
            try:
                client, _ = await self._worker(adopted, child)
                listed.append(self._status(client, child))
            except Exception:
                listed.append(
                    {
                        "session": child.uri,
                        "title": child.title,
                        "machine": child.node,
                        "status": "unreachable",
                    }
                )
        return listed

    # ─── the flow tools ──────────────────────────────────────────────────

    def _flow_tool_host(self, client: Client, adopted: _Adopted) -> ClientToolHost:
        host = ClientToolHost(client.protocol, client_id=client.client_id)
        runners: dict[str, Callable[[_Adopted, dict[str, Any], Mapping[str, Any]], Awaitable[Any]]]
        runners = {
            "start_session": self._flow_start,
            "wait_for_sessions": self._flow_wait,
            "read_session": self._flow_read,
            "stop_session": self._flow_stop,
        }
        for definition in FLOW_TOOLS:
            host.register(definition, self._flow_executor(adopted, runners[definition["name"]]))
        return host

    def _flow_executor(
        self,
        adopted: _Adopted,
        runner: Callable[[_Adopted, dict[str, Any], Mapping[str, Any]], Awaitable[Any]],
    ) -> Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]:
        async def execute(action: Mapping[str, Any]) -> dict[str, Any]:
            try:
                arguments = _arguments(action.get("toolInput"))
                result = await runner(adopted, arguments, action)
            except (ToolError, AhpError) as exc:
                message = exc.message if isinstance(exc, AhpError) else str(exc)
                return actions.tool_failure_result(message)
            return {
                "success": True,
                "content": [{"type": "text", "text": json.dumps(result)}],
                "structuredContent": result,
            }

        return execute

    def _flow_config(self, requested: Any) -> dict[str, Any]:
        """The config for a started session: `child_config`, tightened never loosened."""
        base_config = self.config.child_config
        if requested is None:
            return dict(base_config)
        if not isinstance(requested, Mapping):
            raise ToolError("config must be an object")
        for key in requested:
            if key not in ("effort", "permissionMode"):
                raise ToolError(f"config key {key!r} is not allowed")
        out = dict(base_config)
        if "permissionMode" in requested:
            req = requested["permissionMode"]
            if not isinstance(req, str) or req not in PERMISSION_ORDER:
                raise ToolError(f"unknown permissionMode {req!r}")
            base = base_config.get("permissionMode")
            if isinstance(base, str) and base in PERMISSION_ORDER:
                allowed = PERMISSION_ORDER.index(req) <= PERMISSION_ORDER.index(base)
            elif base is None:
                allowed = req in ("plan", "default")
            else:
                allowed = req == base
            if not allowed:
                raise ToolError(
                    f"permissionMode {req!r} is looser than this gateway allows "
                    f"({base or 'default'})"
                )
            out["permissionMode"] = req
        if "effort" in requested:
            level = requested["effort"]
            if not isinstance(level, str) or level not in EFFORT_LEVELS:
                raise ToolError(f"unknown effort {level!r}")
            if "effort" not in base_config:
                out["effort"] = level
        return out

    async def _flow_start(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        node_id = _string(arguments, "node")
        agent = _string(arguments, "agent")
        prompt = _string(arguments, "prompt")
        if self.is_orchestrator(agent) or self.is_flow(agent):
            raise ToolError("a fleet call cannot start an orchestrator or flow session")
        config = self._flow_config(arguments.get("config"))
        schema = arguments.get("output_schema")
        if schema is not None and not isinstance(schema, Mapping):
            raise ToolError("output_schema must be an object")
        model = arguments.get("model")
        model = model if isinstance(model, str) and model else None
        folder = arguments.get("folder")
        folder = folder if isinstance(folder, str) and folder else None
        # The delegation the node session names as its origin: this flow session, and the
        # chat and tool call on whose behalf the engine starts it. Both or neither.
        origin_chat = arguments.get("originChat")
        origin_call = arguments.get("originToolCall")
        origin = (
            delegation_origin(adopted.uri, origin_chat, origin_call)
            if isinstance(origin_chat, str)
            and origin_chat
            and isinstance(origin_call, str)
            and origin_call
            else None
        )
        async with adopted.lock:
            # An engine, not a model, is calling: wait for a free slot, don't refuse.
            # The limit is this flow session's, as `max_running` is one orchestrator
            # session's: it bounds one run session (all its runs), alongside the
            # engine's own concurrency cap (ahp-host-flow plan §2), not everything
            # the principal runs.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.config.slot_wait
            while True:
                running = [
                    c
                    for c in list(adopted.children.values())
                    if await self._flow_running(adopted, c)
                ]
                if len(running) < self.config.max_running:
                    break
                if loop.time() >= deadline:
                    raise ToolError(
                        f"{len(running)} of your sessions are still running after waiting "
                        f"{self.config.slot_wait:g}s"
                    )
                await asyncio.sleep(FLOW_POLL)
            # The principal recorded with the flow session: the node is dialed and
            # admitted as whoever started the run.
            client = await self._client(adopted.principal, node_id)
            session = await client.create_session(
                provider=agent,
                uri=f"{agent}:/{uuid.uuid4()}",
                working_directories=[_folder_uri(folder)] if folder else None,
                config=config,
                origin=origin,
            )
            title = arguments.get("title")
            title = title if isinstance(title, str) and title else prompt.splitlines()[0][:60]
            spawned = _Spawned(
                uri=session.uri,
                node=node_id,
                parent=adopted.uri,
                title=title,
                tool_call_id=str(action.get("toolCallId") or "") or None,
            )
            adopted.children[session.uri] = spawned
            self._save()
            chat = await session.chat()
            spawned.chat = chat.uri
            self._save()
            self._sessions[session.uri] = (client, session)
            client.protocol.dispatch(
                chat.uri,
                actions.turn_started(
                    str(uuid.uuid4()),
                    text=prompt,
                    model=model,
                    meta={OUTPUT_SCHEMA_META_KEY: dict(schema)} if schema else None,
                ),
            )
            with contextlib.suppress(Exception):
                client.protocol.dispatch(
                    session.uri, {"type": "session/titleChanged", "title": title}
                )
        return {"session": session.uri, "chat": chat.uri}

    def _flow_status(self, client: Client, child: _Spawned) -> dict[str, Any]:
        """A flow child's status for the engine: `done` says it will not change by itself.

        `waiting` (an approval or question for a person) and `starting` are not
        done: the engine keeps waiting, and the person answers in any client.
        """
        session = client.mirror.state(child.uri)
        if not isinstance(session, Mapping):
            status = "gone"
        else:
            chat = self._chat_state(client, child.uri)
            turns = chat.get("turns") or []
            last = turns[-1] if turns and isinstance(turns[-1], Mapping) else {}
            if session.get("lifecycle") == "creationFailed":
                status = "failed"
            elif session.get("inputNeeded"):
                status = "waiting"
            elif chat.get("activeTurn") is not None:
                status = "working"
            elif not turns:
                status = "starting"
            else:
                status = {"complete": "done", "cancelled": "stopped", "error": "failed"}.get(
                    str(last.get("state")), "starting"
                )
        return {
            "session": child.uri,
            "status": status,
            "done": status in ("done", "stopped", "failed", "gone"),
        }

    async def _flow_running(self, adopted: _Adopted, child: _Spawned) -> bool:
        """Whether a flow child may still be running, for `max_running`.

        Read from the node, not from what this process happens to have open: a
        restarted gateway has opened none of the children its state file names,
        and counting only open ones let a run start `max_running` more beside
        the ones still running. A child not seen finished is opened (once, then
        mirrored) and asked; one whose node cannot be reached within
        `FLOW_OPEN_TIMEOUT`, or that is gone, cannot hold a slot and is not
        counted. One seen finished is remembered as such (and saved), so it is
        never opened again.
        """
        if child.finished:
            return False
        try:
            client, _ = await asyncio.wait_for(self._worker(adopted, child), FLOW_OPEN_TIMEOUT)
        except Exception:
            return False
        if self._flow_status(client, child)["done"]:
            child.finished = True
            self._save()
            return False
        return True

    async def _flow_wait(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        uris = arguments.get("sessions")
        if (
            not isinstance(uris, list)
            or not uris
            or not all(isinstance(u, str) and u for u in uris)
        ):
            raise ToolError("name at least one session")
        children = [self._child(adopted, {"session": uri}) for uri in uris]
        clients: dict[str, Client] = {}
        for child in children:
            with contextlib.suppress(Exception):
                clients[child.uri], _ = await self._worker(adopted, child)
        timeout = arguments.get("timeout")
        timeout = float(timeout) if isinstance(timeout, int | float) and timeout > 0 else 600.0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            statuses = [
                self._flow_status(clients[c.uri], c)
                if c.uri in clients
                else {"session": c.uri, "status": "gone", "done": True}
                for c in children
            ]
            finished = all(s["done"] for s in statuses)
            if finished or loop.time() >= deadline:
                return {"sessions": statuses, "timed_out": not finished}
            await asyncio.sleep(FLOW_POLL)

    async def _flow_read(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        child = self._child(adopted, arguments)
        try:
            client, _ = await self._worker(adopted, child)
        except Exception as exc:
            raise ToolError(f"{child.uri} is gone: {exc}") from exc
        chat = self._chat_state(client, child.uri)
        active = chat.get("activeTurn")
        turns = [t for t in chat.get("turns") or [] if isinstance(t, Mapping)]
        turn: Mapping[str, Any] = (
            active if isinstance(active, Mapping) else (turns[-1] if turns else {})
        )
        parts = [p for p in turn.get("responseParts") or [] if isinstance(p, Mapping)]
        text = "".join(
            p["content"]
            for p in parts
            if p.get("kind") == "markdown" and isinstance(p.get("content"), str)
        )
        structured: Any = None
        message = turn.get("message")
        meta = message.get("_meta") if isinstance(message, Mapping) else None
        schema = meta.get(OUTPUT_SCHEMA_META_KEY) if isinstance(meta, Mapping) else None
        if isinstance(schema, Mapping):
            for part in reversed(parts):
                call = part.get("toolCall") if part.get("kind") == "toolCall" else None
                if not isinstance(call, Mapping):
                    continue
                call_meta = call.get("_meta")
                content = call.get("structuredContent")
                if (
                    call.get("toolName") == STRUCTURED_OUTPUT_TOOL
                    and call.get("status") == "completed"
                    and call.get("success") is True
                    and isinstance(call_meta, Mapping)
                    and call_meta.get(STRUCTURED_OUTPUT_TOOL_META_KEY) is True
                    and isinstance(content, Mapping)
                ):
                    structured = unwrap(content, schema)
                    break
        raw_usage = turn.get("usage")
        usage = raw_usage if isinstance(raw_usage, Mapping) else {}
        out: dict[str, Any] = {
            "session": child.uri,
            "status": self._flow_status(client, child)["status"],
            "text": text,
            "structured": structured,
            "usage": {
                "input_tokens": int(usage.get("inputTokens") or 0),
                "output_tokens": int(usage.get("outputTokens") or 0),
            },
        }
        if turn.get("state") == "error":
            error = turn.get("error")
            reason = error.get("message") if isinstance(error, Mapping) else None
            if not reason:
                reason = next(
                    (p.get("message") for p in reversed(parts) if p.get("kind") == "error"), None
                )
            out["error"] = str(reason or "the turn failed")
        return out

    async def _flow_stop(
        self, adopted: _Adopted, arguments: dict[str, Any], action: Mapping[str, Any]
    ) -> Any:
        child = self._child(adopted, arguments)
        client, session = await self._worker(adopted, child)
        if self._chat_state(client, child.uri).get("activeTurn") is None:
            return {"session": child.uri, "stopped": False}
        await (await session.chat()).cancel()
        return {"session": child.uri, "stopped": True}

    def flow_child_owner(self, channel: str) -> Principal | None:
        """Whose run started a session, when *channel* is one a flow run started or its chat."""
        for adopted in self._adopted.values():
            if adopted.kind != "flow":
                continue
            for child in adopted.children.values():
                if channel in (child.uri, child.chat):
                    return adopted.principal
        return None

    def flow_owner(self, channel: str) -> Principal | None:
        """Whose run a channel belongs to, when it is a flow session or one of its chats.

        A flow session runs as the principal who started it; anyone else
        prompting it would be driving that principal's fleet access.
        """
        for adopted in self._adopted.values():
            if adopted.kind != "flow":
                continue
            if channel == adopted.uri:
                return adopted.principal
            client = self._clients.get((adopted.principal.subject, adopted.node))
            state = client.mirror.state(adopted.uri) if client is not None else None
            if not isinstance(state, Mapping):
                continue
            chats = {c.get("resource") for c in state.get("chats") or [] if isinstance(c, Mapping)}
            if channel == state.get("defaultChat") or channel in chats:
                return adopted.principal
        return None

    # ─── what it started ─────────────────────────────────────────────────

    def spawned_by(self, session_uri: str) -> _Spawned | None:
        """The orchestrator that started this session, if one did."""
        for adopted in self._adopted.values():
            child = adopted.children.get(session_uri)
            if child is not None:
                return child
        return None

    # ─── persistence ─────────────────────────────────────────────────────

    def _save(self) -> None:
        path = self.config.state_path
        if path is None:
            return
        data = {
            "orchestrators": [
                {
                    "uri": a.uri,
                    "kind": a.kind,
                    "node": a.node,
                    "subject": a.principal.subject,
                    "groups": sorted(a.principal.groups),
                    "children": [
                        {
                            "uri": c.uri,
                            "node": c.node,
                            "title": c.title,
                            "toolCallId": c.tool_call_id,
                            "chat": c.chat,
                            "finished": c.finished,
                        }
                        for c in a.children.values()
                    ],
                }
                for a in self._adopted.values()
            ]
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2))
        temporary.replace(path)

    def _load(self) -> list[_Adopted]:
        path = self.config.state_path
        if path is None or not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            _log.exception("could not read %s; starting with no orchestrators", path)
            return []
        loaded: list[_Adopted] = []
        for entry in data.get("orchestrators") or []:
            principal = Principal(entry["subject"], frozenset(entry.get("groups") or ()))
            adopted = _Adopted(
                uri=entry["uri"],
                node=entry["node"],
                principal=principal,
                kind=entry.get("kind") or "orchestrator",
            )
            for child in entry.get("children") or []:
                adopted.children[child["uri"]] = _Spawned(
                    uri=child["uri"],
                    node=child["node"],
                    parent=adopted.uri,
                    title=child.get("title") or child["uri"],
                    tool_call_id=child.get("toolCallId"),
                    chat=child.get("chat"),
                    finished=child.get("finished") is True,
                )
            loaded.append(adopted)
        return loaded


# ─── helpers ─────────────────────────────────────────────────────────────


def _arguments(tool_input: Any) -> dict[str, Any]:
    """`ToolInput` is a JSON string on the wire; a mapping is accepted as given."""
    if isinstance(tool_input, Mapping):
        return dict(tool_input)
    if isinstance(tool_input, str) and tool_input.strip():
        try:
            parsed = json.loads(tool_input)
        except ValueError as exc:
            raise ToolError(f"the tool input is not JSON: {exc}") from exc
        if isinstance(parsed, dict):
            return parsed
    return {}


def _string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} is required")
    return value.strip()


_DRIVE: Final = re.compile(r"^[A-Za-z]:[\\/]")


def _folder_uri(folder: str) -> str:
    """A folder as the node names it: a `file:` URI, from a path or a URI."""
    if folder.startswith("file:"):
        return folder
    if _DRIVE.match(folder):
        # Not `PureWindowsPath.as_uri()`: pathlib deprecated `as_uri` on pure paths
        # (gone in 3.19), and this one must work from any platform's gateway.
        path = PureWindowsPath(folder)
        return "file:///" + path.drive + quote(path.as_posix()[len(path.drive) :])
    if folder.startswith("/"):
        return "file://" + quote(str(PurePosixPath(folder)))
    raise invalid_params(f"{folder!r} is not an absolute path or a file URI")


def _transcript(turn: Mapping[str, Any]) -> list[str]:
    """One turn as lines: the message, then the answer and a line per tool."""
    lines: list[str] = []
    message = turn.get("message")
    text = message.get("text") if isinstance(message, Mapping) else None
    if isinstance(text, str) and text:
        lines.append(f"> {text}")
    for part in turn.get("responseParts") or []:
        if not isinstance(part, Mapping):
            continue
        kind = part.get("kind")
        if kind == "markdown" and isinstance(part.get("content"), str):
            lines.append(part["content"].strip())
        elif kind == "toolCall" and isinstance(part.get("toolCall"), Mapping):
            call = part["toolCall"]
            line = call.get("pastTenseMessage") or call.get("invocationMessage")
            name = call.get("displayName") or call.get("toolName")
            lines.append(f"- [{call.get('status')}] {line or name}")
        elif kind == "error":
            lines.append(f"Error: {part.get('message') or part}")
    if turn.get("state") in ("error", "cancelled"):
        lines.append(f"(turn {turn['state']})")
    return lines
