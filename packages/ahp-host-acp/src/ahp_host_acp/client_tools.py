"""Tools a client offers to run, and the loopback bridge that serves them to ACP.

A client publishes tools on `SessionActiveClient.tools`, and the host's
`TurnSink.run_client_tool` asks it to run one: the call executes in the
client's process, not on this machine. An ACP agent only calls tools it was
given as MCP servers, and every agent must take a `stdio` one, so the tools
are served by `relay.py`, a stdio MCP server the agent launches. It connects
back to `ToolBridge`, a loopback listener in this process, which lists the
tools and runs each call through the provider (and so `run_client_tool`).

The bridge listens on 127.0.0.1 (or the configured `relay_host`), which other
processes can reach, so a connection must open with the session's random token (`token_for`); the
relay gets it in the environment of the server entry, never in argv.

Host tools (the per-turn `StructuredOutput`) ride the same relay: their
`client_id` is `HOST_CLIENT_ID`, which `run_client_tool` runs in the host.

Approval belongs to whoever runs the tool, so `permissions.py` lets calls to
these tools through (`is_relayed_title`): they touch nothing on this machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import secrets
import socket
import sys
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ahp_host.provider.base import HOST_CLIENT_ID, HostTool, ToolResult

from ahp_host_acp.mcp import McpServer
from ahp_host_acp.relay import TOKEN_ENV

log = logging.getLogger(__name__)

#: The MCP server's name, and so part of every tool name the agent sees.
SERVER: Final = "ahp_client"
RELAY_MODULE: Final = "ahp_host_acp.relay"
HANDSHAKE_TIMEOUT: Final = 10.0
#: How long `aclose` waits for the server to finish closing. On Windows the proactor
#: loop can miss a transport's `connection_lost`, and `Server.wait_closed()` (which
#: from Python 3.12 waits for every accepted connection) would then wait for ever.
CLOSE_TIMEOUT: Final = 5.0
MAX_LINE: Final = 16 * 1024 * 1024
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_-]")
_MAX_NAME: Final = 64


@dataclass(frozen=True)
class RelayTool:
    """One tool the relay offers, and which client runs it."""

    #: The name the agent calls it by.
    name: str
    #: The name the client published, which is what it is asked to run.
    published: str
    title: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    client_id: str = field(default="", compare=False)

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description or self.title,
            "inputSchema": dict(self.input_schema),
        }


def _schema(value: Any) -> dict[str, Any]:
    schema = dict(value) if isinstance(value, Mapping) else {}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def offered(clients: Sequence[Mapping[str, Any]]) -> tuple[RelayTool, ...]:
    """Every tool the session's clients publish, each name once.

    The first client in `activeClients` order runs a name two publish. A name
    the agent cannot use is made safe rather than dropped; one that then
    collides with another is dropped, because a call could not say which was
    meant.
    """
    tools: dict[str, RelayTool] = {}
    for client in clients:
        client_id = client.get("clientId")
        published = client.get("tools")
        if not isinstance(client_id, str) or not isinstance(published, list):
            continue
        for tool in published:
            if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str):
                continue
            name = _UNSAFE.sub("_", tool["name"])[:_MAX_NAME]
            if not name or name in tools:
                continue
            title = tool.get("title")
            description = tool.get("description")
            tools[name] = RelayTool(
                name=name,
                published=tool["name"],
                title=title if isinstance(title, str) and title else tool["name"],
                description=description if isinstance(description, str) else "",
                input_schema=_schema(tool.get("inputSchema")),
                client_id=client_id,
            )
    return tuple(tools.values())


def host_relay_tools(tools: Sequence[HostTool]) -> tuple[RelayTool, ...]:
    """The host's per-turn tools, as relay tools run by the host itself."""
    return tuple(
        RelayTool(
            name=tool.name,
            published=tool.name,
            title=tool.title,
            description=tool.description,
            input_schema=_schema(tool.input_schema),
            client_id=HOST_CLIENT_ID,
        )
        for tool in tools
    )


def merged(host: Sequence[RelayTool], clients: Sequence[RelayTool]) -> tuple[RelayTool, ...]:
    """Host tools first; a client tool a host tool shares a name with is dropped."""
    names = {tool.name for tool in host}
    return (*host, *(tool for tool in clients if tool.name not in names))


def titled_tool(title: str, tools: Collection[RelayTool]) -> RelayTool | None:
    """The relay tool an agent's title for a tool call names, if any.

    ACP does not say how an agent titles an MCP tool call, so every spelling
    agents are known to use is matched exactly, never as a suffix (which
    `exec: ahp_client_ping` would satisfy).
    """
    for tool in tools:
        name = tool.name
        if title == f"mcp__{SERVER}__{name}":
            return tool
        if any(title == f"{SERVER}{sep}{name}" for sep in ("_", "__", "/", ":", ".", " ")):
            return tool
    return None


def is_relayed_title(title: str, names: Collection[str]) -> bool:
    """Whether an agent's title for a tool call names one of these tools."""
    return titled_tool(title, [RelayTool(n, n, n, "") for n in names]) is not None


#: ACP tool kinds a relayed MCP call can carry. Agents report an MCP tool call
#: as `other`, or with no kind; anything that acts on this machine (`execute`,
#: `edit`, `delete`, `move`, `read`, `fetch`, ...) is never a relayed call.
RELAYED_KINDS: Final = frozenset({"other"})


def looks_relayed(update: Mapping[str, Any], tools: Collection[RelayTool]) -> bool:
    """Whether a permission request is for a call to one of the relay's tools.

    **A security check.** The title alone is not enough: harnesses often title
    a call with text the model chose (a shell command's description, say), so a
    prompt-injected model could title `rm -rf ~` as `ahp_client_StructuredOutput`.
    So the call must also have no kind or kind `other`, and its raw input, when
    the agent reports one, must have that tool's shape: an object using only the
    properties the tool's schema declares. The values are not checked here: a
    wrong value is the tool owner's to refuse (StructuredOutput's mismatch goes
    back to the agent as a retry), not a sign of a disguised local call.
    Whatever fails these is asked about like any other call.
    """
    title = update.get("title")
    if not isinstance(title, str):
        return False
    tool = titled_tool(title, tools)
    if tool is None:
        return False
    kind = update.get("kind")
    if kind is not None and kind not in RELAYED_KINDS:
        return False
    if "rawInput" not in update:
        return True
    raw = update["rawInput"]
    if not isinstance(raw, Mapping):
        return False
    declared = tool.input_schema.get("properties")
    return not isinstance(declared, Mapping) or set(raw) <= set(declared)


def error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def mcp_result(result: ToolResult) -> dict[str, Any]:
    """The client's `ToolCallResult`, as the MCP `CallToolResult` the agent reads."""
    if not result.accepted:
        return error(result.reason or "The client did not run this tool.")
    value = result.value if isinstance(result.value, Mapping) else {}
    texts: list[str] = []
    for part in value.get("content") or ():
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "text":
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
        else:
            texts.append(json.dumps(dict(part)))
    if not texts and value.get("structuredContent") is not None:
        texts.append(json.dumps(value["structuredContent"]))
    if not texts:
        past = value.get("pastTenseMessage")
        texts.append(past if isinstance(past, str) and past else "Done.")
    return {"content": [{"type": "text", "text": text} for text in texts], "isError": False}


ToolsFor = Callable[[str], Sequence[RelayTool]]
Call = Callable[[str, RelayTool, dict[str, Any]], Awaitable[dict[str, Any]]]


class _Connection:
    """One relay's socket; writes are serialised."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        async with self._lock:
            try:
                self._writer.write(_line(message))
                await self._writer.drain()
            except (ConnectionError, OSError):
                pass

    def notify(self, message: dict[str, Any]) -> None:
        with contextlib.suppress(ConnectionError, OSError, RuntimeError):
            self._writer.write(_line(message))

    def close(self) -> None:
        with contextlib.suppress(ConnectionError, OSError, RuntimeError):
            self._writer.close()


def _line(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=True) + "\n").encode("ascii")


class ToolBridge:
    """The listener the relays connect back to, one key per session.

    It listens on loopback unless `host` names an address (a container's
    bridge gateway, say); `relay_command` replaces the relay's command for a
    relay that runs where this machine's Python is not (inside a container).
    """

    def __init__(
        self,
        tools_for: ToolsFor,
        call: Call,
        *,
        host: str | None = None,
        relay_command: Sequence[str] | None = None,
    ) -> None:
        self._tools_for = tools_for
        self._call = call
        self._host = host
        self._relay_command = tuple(relay_command) if relay_command else None
        self._server: asyncio.Server | None = None
        self._tokens: dict[str, str] = {}
        self._connections: dict[str, set[_Connection]] = {}
        #: Every accepted connection, including one still in its handshake.
        self._accepted: set[_Connection] = set()

    async def start(self) -> None:
        if self._server is None:
            address = "127.0.0.1"
            if self._host is not None:
                # One address, so one socket and one port: binding a name can
                # open a socket per address, each with its own port.
                found = await asyncio.get_running_loop().getaddrinfo(
                    self._host, None, type=socket.SOCK_STREAM
                )
                address = str(found[0][4][0])
            self._server = await asyncio.start_server(self._serve, address, 0, limit=MAX_LINE)

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("the bridge is not started")
        return int(self._server.sockets[0].getsockname()[1])

    def token_for(self, key: str) -> str:
        if key not in self._tokens:
            self._tokens[key] = secrets.token_urlsafe(32)
        return self._tokens[key]

    def revoke(self, key: str) -> None:
        """Forget *key*'s token and close its relays: the chat is gone, and a
        relay the agent still holds for it must not keep reaching the tools."""
        self._tokens.pop(key, None)
        for connection in list(self._connections.pop(key, ())):
            connection.close()

    def server(self, key: str) -> McpServer:
        """The MCP server entry that gives an agent this key's tools."""
        base = self._relay_command or (sys.executable, "-m", RELAY_MODULE)
        command = (*base, "--port", str(self.port))
        if self._host is not None:
            command = (*command, "--host", self._host)
        return McpServer(
            name=SERVER,
            transport="stdio",
            command=command,
            env={TOKEN_ENV: self.token_for(key)},
            resolve=self._relay_command is None,
        )

    def tools_changed(self, key: str | None = None) -> None:
        """Tell the relays of one key (every key if None) to re-list their tools."""
        keys = list(self._connections) if key is None else [key]
        for each in keys:
            for connection in list(self._connections.get(each, ())):
                connection.notify({"method": "tools/list_changed"})

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
        # Close the connections before waiting: from Python 3.12 wait_closed()
        # also waits for every open connection, which a live relay would hold open.
        for connection in list(self._accepted):
            connection.close()
        self._accepted.clear()
        self._connections.clear()
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), CLOSE_TIMEOUT)
            except TimeoutError:
                log.warning("the client-tool bridge did not finish closing in %ss", CLOSE_TIMEOUT)
            self._server = None

    def _key_for(self, token: str) -> str | None:
        found = None
        for key, expected in self._tokens.items():
            if hmac.compare_digest(expected.encode(), token.encode()):
                found = key
        return found

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = _Connection(writer)
        self._accepted.add(connection)
        key: str | None = None
        tasks: set[asyncio.Task[None]] = set()
        try:
            try:
                first = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT)
                hello = json.loads(first)
            except (TimeoutError, ValueError):
                return
            token = hello.get("token") if isinstance(hello, dict) else None
            key = self._key_for(token) if isinstance(token, str) else None
            if key is None:
                return
            await connection.send({"ok": True})
            self._connections.setdefault(key, set()).add(connection)
            while line := await reader.readline():
                try:
                    request = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(request, dict) or "id" not in request:
                    continue
                task = asyncio.create_task(self._handle(key, connection, request))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if key is not None:
                self._connections.get(key, set()).discard(connection)
            self._accepted.discard(connection)
            connection.close()

    async def _handle(self, key: str, connection: _Connection, request: dict[str, Any]) -> None:
        method = request.get("method")
        raw = request.get("params")
        params: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        answer: dict[str, Any]
        if method == "tools/list":
            answer = {"result": {"tools": [t.to_mcp() for t in self._tools_for(key)]}}
        elif method == "tools/call":
            name = params.get("name")
            tool = next((t for t in self._tools_for(key) if t.name == name), None)
            if tool is None:
                answer = {"result": error(f"No tool named {name!r} is offered.")}
            else:
                arguments = params.get("arguments")
                try:
                    result = await self._call(
                        key, tool, dict(arguments) if isinstance(arguments, Mapping) else {}
                    )
                except Exception as exc:
                    log.warning("relayed tool %s failed: %s", tool.name, exc)
                    result = error(str(exc))
                answer = {"result": result}
        else:
            answer = {"error": {"message": f"no {method}"}}
        await connection.send({"id": request["id"], **answer})
