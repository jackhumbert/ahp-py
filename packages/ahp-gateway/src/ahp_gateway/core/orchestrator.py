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

from ahp_gateway.registry import NodeRecord, Principal

if TYPE_CHECKING:
    from ahp_gateway.core.gateway import Gateway

__all__ = ["FLEET_TOOLS", "Orchestrator", "OrchestratorConfig"]

_log = logging.getLogger(__name__)

#: `_meta` key on a spawned session's summary and on each fleet tool call's
#: result, naming the orchestrator session that started it.
SPAWNED_BY_META_KEY: Final = "ahp-gateway/spawnedBy"


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


@dataclass
class _Spawned:
    """A session an orchestrator started."""

    uri: str
    node: str
    parent: str
    title: str
    tool_call_id: str | None = None


@dataclass
class _Adopted:
    """An orchestrator session, and whose it is."""

    uri: str
    node: str
    principal: Principal
    tools: ClientToolHost | None = None
    children: dict[str, _Spawned] = field(default_factory=dict)


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
                adopted.tools = self._tool_host(client, adopted)
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

    async def create(self, principal: Principal, node_id: str, params: Mapping[str, Any]) -> None:
        """Start an orchestrator session where the surface asked for one.

        Created by the orchestrator's own connection, on the surface's own
        session URI, so the surface subscribes to it as it would to any session
        it created, and the fleet tools are there from the first turn.
        """
        channel = str(params["channel"])
        client = await self._client(principal, node_id)
        adopted = _Adopted(uri=channel, node=node_id, principal=principal)
        adopted.tools = self._tool_host(client, adopted)
        directories = params.get("workingDirectories")
        config = params.get("config")
        await client.create_session(
            provider=self.config.provider,
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
                    "node": a.node,
                    "subject": a.principal.subject,
                    "groups": sorted(a.principal.groups),
                    "children": [
                        {
                            "uri": c.uri,
                            "node": c.node,
                            "title": c.title,
                            "toolCallId": c.tool_call_id,
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
            adopted = _Adopted(uri=entry["uri"], node=entry["node"], principal=principal)
            for child in entry.get("children") or []:
                adopted.children[child["uri"]] = _Spawned(
                    uri=child["uri"],
                    node=child["node"],
                    parent=adopted.uri,
                    title=child.get("title") or child["uri"],
                    tool_call_id=child.get("toolCallId"),
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
        return PureWindowsPath(folder).as_uri()
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
