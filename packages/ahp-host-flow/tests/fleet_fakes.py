"""An in-memory fleet for the fleet dispatch tests; not a test module.

`FakeFleet` stands in for a gateway's node sessions, and `join_fake_gateway` joins a client
to a flow session offering the four fleet tools over it, as the gateway would.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import ahp_client
from ahp_client import Client
from ahp_client.client import actions
from ahp_client.serve import ClientToolHost
from ahp_host import Host
from ahp_protocol.transport import memory_pair

from ahp_host_flow.fleet import FLEET_TOOLS
from tests.preamble import strip_preamble

_DONE = ("done", "stopped", "failed", "gone")
_SERVING: set[asyncio.Task[None]] = set()


class FakeFleet:
    """Node sessions that finish at once, except a prompt starting `hold` which waits."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.starts: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self._forgotten: set[str] = set()
        #: `read_session` never answers, as over a link that died without a word.
        self.hang_reads = False
        #: `wait_for_sessions` calls in flight, so a test can act while one is.
        self.waiting = 0

    def start(self, args: Mapping[str, Any]) -> dict[str, Any]:
        self.starts.append(dict(args))
        node, agent, prompt = (
            str(args["node"]),
            str(args["agent"]),
            strip_preamble(str(args["prompt"])),
        )
        uri = f"{agent}:/fake-{len(self.sessions)}"
        schema = args.get("output_schema")
        entry: dict[str, Any] = {
            "node": node,
            "agent": agent,
            "prompt": prompt,
            "schema": schema,
            "config": args.get("config"),
            "status": "working",
            "text": "",
            "structured": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        self.sessions[uri] = entry
        if not prompt.startswith("hold"):
            entry["status"] = "done"
            entry["text"] = f"{node}: {prompt}"
            if schema is not None:
                try:
                    entry["structured"] = json.loads(prompt)
                except ValueError:
                    entry["structured"] = None
            entry["usage"] = {"input_tokens": 3, "output_tokens": 5}
        return {"session": uri, "chat": f"{uri}/chat"}

    def release(self, uri: str, text: str) -> None:
        """Finish a held session with *text*."""
        entry = self.sessions[uri]
        entry.update(status="done", text=text, usage={"input_tokens": 3, "output_tokens": 5})

    def forget(self, uri: str) -> None:
        """Make the session look gone to `wait_for_sessions` and `read_session`."""
        self._forgotten.add(uri)

    def status(self, uri: str) -> str:
        if uri in self._forgotten or uri not in self.sessions:
            return "gone"
        return str(self.sessions[uri]["status"])

    async def wait(self, args: Mapping[str, Any]) -> dict[str, Any]:
        uris = [str(u) for u in args["sessions"]]
        timeout = float(args.get("timeout") or 0)
        deadline = asyncio.get_running_loop().time() + timeout
        self.waiting += 1
        try:
            while not all(self.status(u) in _DONE for u in uris):
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.01)
        finally:
            self.waiting -= 1
        statuses = [
            {"session": u, "status": self.status(u), "done": self.status(u) in _DONE} for u in uris
        ]
        return {"sessions": statuses, "timed_out": not all(s["done"] for s in statuses)}

    def read(self, args: Mapping[str, Any]) -> dict[str, Any]:
        uri = str(args["session"])
        status = self.status(uri)
        entry = self.sessions.get(uri)
        if entry is None or status == "gone":
            return {
                "session": uri,
                "status": "gone",
                "text": "",
                "structured": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "error": "the session is gone",
            }
        return {
            "session": uri,
            "status": status,
            "text": entry["text"],
            "structured": entry["structured"],
            "usage": entry["usage"],
        }

    def stop(self, args: Mapping[str, Any]) -> dict[str, Any]:
        uri = str(args["session"])
        self.stopped.append(uri)
        entry = self.sessions.get(uri)
        stopped = entry is not None and entry["status"] == "working"
        if entry is not None and stopped:
            entry["status"] = "stopped"
        return {"session": uri, "stopped": stopped}


def _arguments(action: Mapping[str, Any]) -> dict[str, Any]:
    tool_input = action.get("toolInput")
    if isinstance(tool_input, Mapping):
        return dict(tool_input)
    if isinstance(tool_input, str) and tool_input.strip():
        parsed = json.loads(tool_input)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _executor(fleet: FakeFleet, name: str) -> Any:
    async def execute(action: Mapping[str, Any]) -> Mapping[str, Any]:
        arguments = _arguments(action)
        try:
            if name == "start_session":
                data = fleet.start(arguments)
            elif name == "wait_for_sessions":
                data = await fleet.wait(arguments)
            elif name == "read_session":
                if fleet.hang_reads:
                    await asyncio.Event().wait()
                data = fleet.read(arguments)
            else:
                data = fleet.stop(arguments)
        except (KeyError, ValueError) as exc:
            return actions.tool_failure_result(f"{name}: {exc}")
        return {
            "success": True,
            "pastTenseMessage": f"Ran {name}",
            "content": [{"type": "text", "text": json.dumps(data)}],
            "structuredContent": data,
        }

    return execute


async def join_fake_gateway(
    host: Host, session_uri: str, fleet: FakeFleet, *, client_id: str = "fake-gateway"
) -> Client:
    """A client joined to *session_uri* as an active client offering the fleet tools."""
    client_end, server_end = memory_pair()
    serving = asyncio.create_task(host.serve(server_end))
    _SERVING.add(serving)
    serving.add_done_callback(_SERVING.discard)
    client: Client = await ahp_client.connect(
        transport=client_end, reconnect=False, client_id=client_id
    )
    tools = ClientToolHost(client.protocol, client_id=client.client_id)
    for name in FLEET_TOOLS:
        tools.register(
            {
                "name": name,
                "description": f"Fake fleet tool {name}",
                "inputSchema": {"type": "object"},
            },
            _executor(fleet, name),
        )
    session = await client.open_session(session_uri, tools=tools)
    # The pump answers calls on the chats it has mirrored.
    await session.chat()
    watching = asyncio.create_task(_follow_chats(client, session))
    _SERVING.add(watching)
    watching.add_done_callback(_SERVING.discard)
    return client


async def _follow_chats(client: Client, session: Any) -> None:
    """Subscribe to the session's worker chats too, so a call made from one is answered."""
    seen = {str(session.state.get("defaultChat") or "")}
    while True:
        for chat in session.chats():
            uri = str(chat.get("resource") or "")
            if uri and uri not in seen:
                seen.add(uri)
                try:
                    await client._runtime.subscribe(uri, "chat")
                except Exception:
                    return  # the client or its host has gone
        await asyncio.sleep(0.01)
