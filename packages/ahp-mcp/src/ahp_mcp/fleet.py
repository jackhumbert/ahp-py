"""The AHP half: one supervised client connection, and what the tools do with it.

Nothing here knows MCP exists. Each method takes and returns plain data, so the
tool layer (`ahp_mcp.server`) is only names, schemas and descriptions.

**Two shapes of endpoint.** Behind an `ahp-gateway` the machines are its
nodes: `ahp-file:///` lists them, a node's root is `ahp-file:///<node>`, and an
absolute path on one is `ahp-file://<node>/<path>`. The gateway routes a new
session to the node its working directory names, so naming the folder is
naming the machine. A lone host is one machine, called `host`, whose files are
plain `file:` URIs. Which it is is read off the handshake: a gateway advertises
its `defaultDirectory` under `ahp-file:`.

**Whose sessions.** The connection is the owner's own (one token, one
principal at the gateway), so every session it can list is theirs, and the
tools may read, prompt or stop any of them, not only the ones started here.
They never answer an approval: there is no tool for it, and a question a
session raises waits for a person in any AHP client.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Final
from urllib.parse import quote, unquote

from ahp_client import Client, Session
from ahp_client.api import ClientContext
from ahp_client.client import actions
from ahp_protocol import AhpError

__all__ = ["LONE_HOST", "Fleet", "FleetError", "folder_uri"]

#: What a lone host (no gateway in front) is called in the tools.
LONE_HOST: Final = "host"

_GATEWAY_SCHEME: Final = "ahp-file"
_GATEWAY_ROOT: Final = f"{_GATEWAY_SCHEME}:///"
#: Between node id and root name in an `ahp-gateway` flat place.
_PLACE_SEPARATOR: Final = ": "
_DRIVE: Final = re.compile(r"^[A-Za-z]:[\\/]")
_PATH_SAFE: Final = "/:@!$&'()*+,;=-._~"

#: Longest a single `wait_for_session` may block: a tool call has a client-side
#: timeout too, and a call that outlives it is an error to the model.
MAX_WAIT_SECONDS: Final = 300.0
_POLL_SECONDS: Final = 0.25


class FleetError(Exception):
    """A tool's own refusal, worded for the model that called it."""


Connector = Callable[[], ClientContext]


@dataclass
class Fleet:
    """The tools' state: the connection, and the sessions opened on it.

    *connect* returns a fresh `ahp_client.connect(...)` context; it is called
    once, on first use, so the server starts even while the endpoint is down.
    *session_config* is given to every session started here (for a Claude
    host, `{"permissionMode": "auto"}` lets it work without an approval per
    step); the model cannot change it. *default_agent* is used when a start
    names none and the endpoint offers more than one.
    """

    connect: Connector
    session_config: Mapping[str, Any] = field(default_factory=dict)
    default_agent: str | None = None
    connect_timeout: float = 15.0
    _context: ClientContext | None = field(default=None, init=False, repr=False)
    _client: Client | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _sessions: dict[str, Session] = field(default_factory=dict, init=False, repr=False)

    # ─── the connection ──────────────────────────────────────────────────

    async def client(self) -> Client:
        async with self._lock:
            if self._client is None:
                context = self.connect()
                try:
                    self._client = await asyncio.wait_for(
                        context.__aenter__(), self.connect_timeout
                    )
                except (TimeoutError, OSError, AhpError) as exc:
                    with contextlib.suppress(Exception):
                        await context.__aexit__(None, None, None)
                    raise FleetError(f"the AHP endpoint is not reachable: {_reason(exc)}") from exc
                self._context = context
            client = self._client
            assert client is not None
            return client

    async def aclose(self) -> None:
        async with self._lock:
            context, self._context, self._client = self._context, None, None
            self._sessions.clear()
            if context is not None:
                await context.__aexit__(None, None, None)

    def _is_gateway(self, client: Client) -> bool:
        directory = client.default_directory or ""
        return directory.lower().startswith(f"{_GATEWAY_SCHEME}:")

    # ─── machines and folders ────────────────────────────────────────────

    async def list_hosts(self) -> dict[str, Any]:
        """The machines, and the agents a session can be started with."""
        client = await self.client()
        agents = _agents(client)
        if not self._is_gateway(client):
            return {
                "hosts": [{"host": LONE_HOST, "root": client.default_directory}],
                "agents": agents,
            }
        listed = await client.protocol.resource_list(_GATEWAY_ROOT)
        hosts: dict[str, dict[str, Any]] = {}
        for entry in listed.get("entries") or []:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
                continue
            node, _, place = entry["name"].partition(_PLACE_SEPARATOR)
            host = hosts.setdefault(node, {"host": node, "root": f"{_GATEWAY_ROOT}{quote(node)}"})
            if place:
                host.setdefault("places", []).append(place)
        return {"hosts": list(hosts.values()), "agents": agents}

    async def list_folder(self, host: str, folder: str | None = None) -> list[dict[str, str]]:
        client = await self.client()
        uri = self._folder(client, await self._host(client, host), folder)
        listed = await client.protocol.resource_list(uri)
        base = uri.rstrip("/")
        return [
            {
                "name": str(entry.get("name")),
                "type": str(entry.get("type")),
                "uri": f"{base}/{quote(str(entry.get('name')), safe=_PATH_SAFE)}",
            }
            for entry in listed.get("entries") or []
            if isinstance(entry, Mapping)
        ]

    async def _host(self, client: Client, host: str) -> str:
        host = host.strip()
        known = [h["host"] for h in (await self.list_hosts())["hosts"]]
        if host not in known:
            raise FleetError(f"there is no host {host!r}; the hosts are: {', '.join(known)}")
        return host

    def _folder(self, client: Client, host: str, folder: str | None) -> str:
        if self._is_gateway(client):
            return folder_uri(folder, host)
        return folder_uri(folder, None, default=client.default_directory)

    # ─── sessions ────────────────────────────────────────────────────────

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        client = await self.client()
        gateway = self._is_gateway(client)
        page = await client.sessions(limit=max(1, min(limit, 200)))
        listed = []
        for item in page.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            directories = [d for d in item.get("workingDirectories") or [] if isinstance(d, str)]
            entry: dict[str, Any] = {
                "session": item.get("resource"),
                "title": item.get("title"),
                "agent": item.get("provider"),
                "modified": item.get("modifiedAt"),
            }
            if directories:
                entry["folder"] = directories[0]
                entry["host"] = _node_of(directories[0]) if gateway else LONE_HOST
            listed.append(entry)
        return listed

    async def start_session(
        self,
        host: str,
        prompt: str,
        *,
        folder: str | None = None,
        agent: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        prompt = _required(prompt, "prompt")
        client = await self.client()
        host = await self._host(client, host)
        provider = self._agent(client, agent)
        directory = self._folder(client, host, folder)
        try:
            session = await client.create_session(
                provider=provider,
                uri=f"{provider}:/{uuid.uuid4()}",
                working_directories=[directory],
                config=dict(self.session_config) or None,
            )
        except AhpError as exc:
            raise FleetError(f"the session did not start: {exc.message}") from exc
        self._sessions[session.uri] = session
        name = (title or "").strip() or prompt.splitlines()[0][:60].strip()
        with contextlib.suppress(Exception):
            client.protocol.dispatch(session.uri, {"type": "session/titleChanged", "title": name})
        chat = await session.chat()
        client.protocol.dispatch(chat.uri, actions.turn_started(str(uuid.uuid4()), text=prompt))
        return {"session": session.uri, "host": host, "folder": directory, "agent": provider}

    def _agent(self, client: Client, agent: str | None) -> str:
        offered = _agents(client)
        if agent:
            if agent not in offered:
                raise FleetError(f"there is no agent {agent!r}; the agents are: {_list(offered)}")
            return agent
        if self.default_agent and self.default_agent in offered:
            return self.default_agent
        if len(offered) == 1:
            return offered[0]
        raise FleetError(f"name an agent; the agents are: {_list(offered)}")

    async def _session(self, uri: str) -> tuple[Client, Session]:
        uri = _required(uri, "session")
        client = await self.client()
        session = self._sessions.get(uri)
        if session is None:
            try:
                session = await client.open_session(uri)
                await session.chat()
            except AhpError as exc:
                raise FleetError(f"cannot open {uri}: {exc.message}") from exc
            self._sessions[uri] = session
        return client, session

    async def send_prompt(self, uri: str, prompt: str) -> str:
        prompt = _required(prompt, "prompt")
        client, session = await self._session(uri)
        if _chat_state(client, uri).get("activeTurn") is not None:
            raise FleetError("that session is busy: wait for it, or cancel its turn first")
        chat = await session.chat()
        client.protocol.dispatch(chat.uri, actions.turn_started(str(uuid.uuid4()), text=prompt))
        return f"Sent to {uri}."

    async def read_session(self, uri: str, turns: int = 2) -> dict[str, Any]:
        client, _ = await self._session(uri)
        chat = _chat_state(client, uri)
        recent = [t for t in chat.get("turns") or [] if isinstance(t, Mapping)]
        active = chat.get("activeTurn")
        if isinstance(active, Mapping):
            recent.append(active)
        lines: list[str] = []
        for turn in recent[-max(1, turns) :]:
            if lines:
                lines.append("")
            lines.extend(transcript(turn))
        return {**status(client, uri), "transcript": "\n".join(lines)}

    async def wait_for_session(self, uri: str, timeout_seconds: float = 60.0) -> dict[str, Any]:
        """Until the session stops working (done, failed, stopped or waiting on a person)."""
        client, _ = await self._session(uri)
        timeout = min(max(timeout_seconds, 0.0), MAX_WAIT_SECONDS)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            current = status(client, uri)
            if current["status"] != "working":
                return {**current, "timed_out": False}
            if loop.time() >= deadline:
                return {**current, "timed_out": True}
            await asyncio.sleep(_POLL_SECONDS)

    async def cancel_session(self, uri: str) -> str:
        client, session = await self._session(uri)
        if _chat_state(client, uri).get("activeTurn") is None:
            return f"{uri} is not running a turn."
        chat = await session.chat()
        await chat.cancel()
        return f"Cancelled the turn {uri} was running."


# ─── reading the mirror ──────────────────────────────────────────────────
#
# The same reading of session and chat state as the gateway's orchestrator
# tools. Copied, not imported: this package is a client, and the gateway is a
# sibling it must not depend on.


def _chat_state(client: Client, session_uri: str) -> Mapping[str, Any]:
    session = client.mirror.state(session_uri)
    chat = session.get("defaultChat") if isinstance(session, Mapping) else None
    state = client.mirror.state(chat) if isinstance(chat, str) else None
    return state if isinstance(state, Mapping) else {}


def status(client: Client, session_uri: str) -> dict[str, Any]:
    """One session's state in a word, and what it waits on if anything."""
    session = client.mirror.state(session_uri)
    session = session if isinstance(session, Mapping) else {}
    chat = _chat_state(client, session_uri)
    waiting = [
        str(entry.get("kind"))
        for entry in session.get("inputNeeded") or []
        if isinstance(entry, Mapping)
    ]
    turns = chat.get("turns") or []
    last = turns[-1] if turns and isinstance(turns[-1], Mapping) else {}
    if waiting:
        word = "waiting on the user"
    elif chat.get("activeTurn") is not None:
        word = "working"
    elif session.get("lifecycle") == "creationFailed":
        word = "failed to start"
    else:
        word = {"complete": "done", "cancelled": "stopped", "error": "failed"}.get(
            str(last.get("state")), "idle"
        )
    entry: dict[str, Any] = {"session": session_uri, "status": word}
    title = session.get("title")
    if isinstance(title, str) and title:
        entry["title"] = title
    if waiting:
        entry["waiting_on"] = waiting
    return entry


def transcript(turn: Mapping[str, Any]) -> list[str]:
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
            tool = part["toolCall"]
            line = tool.get("pastTenseMessage") or tool.get("invocationMessage")
            name = tool.get("displayName") or tool.get("toolName")
            lines.append(f"- [{tool.get('status')}] {line or name}")
        elif kind == "error":
            lines.append(f"Error: {part.get('message') or part}")
    if turn.get("state") in ("error", "cancelled"):
        lines.append(f"(turn {turn['state']})")
    return lines


# ─── folders ─────────────────────────────────────────────────────────────


def folder_uri(folder: str | None, host: str | None, *, default: str | None = None) -> str:
    """A folder as the endpoint names it.

    With *host* (behind a gateway): nothing is the host's root, a relative path
    is under that root, an absolute path is anywhere on the host, and a URI
    (`ahp-file:` from a listing, or `file:`) is taken as given. Without one (a
    lone host): `file:` URIs, a relative path joined to *default*.
    """
    folder = (folder or "").strip()
    if folder.lower().startswith((f"{_GATEWAY_SCHEME}:", "file:")):
        return folder
    if host is not None:
        if not folder:
            return f"{_GATEWAY_ROOT}{quote(host)}"
        if _DRIVE.match(folder):
            return f"{_GATEWAY_SCHEME}://{quote(host)}/{_windows(folder)}"
        if folder.startswith("/"):
            path = str(PurePosixPath(folder)).lstrip("/")
            return f"{_GATEWAY_SCHEME}://{quote(host)}/{quote(path, safe=_PATH_SAFE)}"
        _no_climbing(folder)
        return f"{_GATEWAY_ROOT}{quote(host)}/{quote(folder.strip('/'), safe=_PATH_SAFE)}"
    if not folder:
        if default is None:
            raise FleetError("this host advertises no default folder; name one")
        return default
    if _DRIVE.match(folder):
        return f"file:///{_windows(folder)}"
    if folder.startswith("/"):
        return "file://" + quote(str(PurePosixPath(folder)), safe=_PATH_SAFE)
    if default is None:
        raise FleetError(f"{folder!r} is relative and this host advertises no default folder")
    _no_climbing(folder)
    return f"{default.rstrip('/')}/{quote(folder.strip('/'), safe=_PATH_SAFE)}"


def _windows(folder: str) -> str:
    path = PureWindowsPath(folder)
    return path.drive + quote(path.as_posix()[len(path.drive) :], safe=_PATH_SAFE)


def _no_climbing(folder: str) -> None:
    if ".." in folder.replace("\\", "/").split("/"):
        raise FleetError(f"{folder!r} climbs out of the root; give an absolute path instead")


def _node_of(uri: str) -> str | None:
    """The gateway node an `ahp-file` URI names (both spellings, flat places too)."""
    prefix = f"{_GATEWAY_SCHEME}://"
    if not uri.lower().startswith(prefix):
        return None
    rest = uri[len(prefix) :].lstrip("/")
    node = unquote(rest.partition("/")[0])
    return node.partition(_PLACE_SEPARATOR)[0] or None


def _agents(client: Client) -> list[str]:
    return [str(a["provider"]) for a in client.agents() if a.get("provider")]


def _list(names: Sequence[str]) -> str:
    return ", ".join(names) or "(none)"


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FleetError(f"{name} is required")
    return value.strip()


def _reason(exc: BaseException) -> str:
    if isinstance(exc, AhpError):
        return exc.message
    return str(exc) or type(exc).__name__
