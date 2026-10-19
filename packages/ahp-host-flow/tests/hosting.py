"""A real `Host` and one in-memory client, for this package's integration tests.

Not a test module. The provider's surfaces are only proven through the host
doing the thing -- a worker chat opened, a turn routed by its chat. A
background reader keeps every frame, so a test waits on a condition rather
than sleeping.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ahp_host import ROOT_URI, Host
from ahp_protocol.transport import memory_pair
from ahp_protocol.transport.memory import MemoryTransport


class Wire:
    """One client connection to *host*."""

    def __init__(self, host: Host, transport: MemoryTransport) -> None:
        self.host = host
        self._transport = transport
        self.frames: list[dict[str, Any]] = []
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._client_seq = 0
        self.initialized: dict[str, Any] = {}
        #: What this client serves to the host's `resourceList` / `resourceRead`: uri -> text.
        self.resources: dict[str, str] = {}
        #: '<method> <uri>' of every resource request the host made.
        self.served: list[str] = []
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        while (message := await self._transport.receive()) is not None:
            request_id = message.get("id")
            if "method" in message and request_id is not None:
                await self._transport.send(
                    {"jsonrpc": "2.0", "id": request_id, **self._answer(message)}
                )
            elif isinstance(request_id, int) and request_id in self._waiting:
                self._waiting.pop(request_id).set_result(dict(message))
            else:
                self.frames.append(dict(message))

    def _answer(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """The result or error for a server->client request."""
        method, params = message["method"], message.get("params") or {}
        uri = params.get("uri")
        if method in ("resourceList", "resourceRead") and isinstance(uri, str):
            self.served.append(f"{method} {uri}")
            if method == "resourceRead" and uri in self.resources:
                return {"result": {"data": self.resources[uri], "encoding": "utf-8"}}
            prefix = uri.rstrip("/") + "/"
            if method == "resourceList":
                entries: dict[str, str] = {}
                for key in self.resources:
                    if key.startswith(prefix):
                        head, sep, _ = key[len(prefix) :].partition("/")
                        entries[head] = "directory" if sep else entries.get(head, "file")
                if entries:
                    return {
                        "result": {
                            "entries": [{"name": n, "type": t} for n, t in sorted(entries.items())]
                        }
                    }
        return {"error": {"code": -32008, "message": "no"}}

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        waiter = asyncio.get_running_loop().create_future()
        self._waiting[self._next_id] = waiter
        await self._transport.send(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": dict(params)}
        )
        return await asyncio.wait_for(waiter, timeout=10)

    async def dispatch(self, channel: str, action: Mapping[str, Any]) -> None:
        self._client_seq += 1
        await self._transport.send(
            {
                "jsonrpc": "2.0",
                "method": "dispatchAction",
                "params": {"channel": channel, "clientSeq": self._client_seq, "action": action},
            }
        )

    async def until(self, ready: Callable[[], bool], timeout: float = 10.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while not ready():
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def close(self) -> None:
        self._reader.cancel()


def flow_tasks() -> list[asyncio.Task[Any]]:
    """Tasks not yet done whose coroutine chain runs code from `ahp_host_flow`."""

    def ours(task: asyncio.Task[Any]) -> bool:
        awaiting: Any = task.get_coro()
        while awaiting is not None:
            code = getattr(awaiting, "cr_code", None) or getattr(awaiting, "gi_code", None)
            if code is not None and "ahp_host_flow" in Path(code.co_filename).parts:
                return True
            awaiting = getattr(awaiting, "cr_await", None) or getattr(
                awaiting, "gi_yieldfrom", None
            )
        return False

    return [task for task in asyncio.all_tasks() if not task.done() and ours(task)]


def state(host: Host, uri: str) -> dict[str, Any]:
    found = host.sequencer.state_of(uri)
    return dict(found) if isinstance(found, Mapping) else {}


def all_channels(host: Host) -> dict[str, str]:
    """Every channel the host's sequencer knows (sessions, chats, root...) as JSON: state and log.

    The sequencer has no public enumeration, so this reads its tables: a scan of "everything
    published" must not depend on remembering which channels exist.
    """
    sequencer = host.sequencer
    channels = {
        uri: json.dumps(st, sort_keys=True, default=str) for uri, st in sequencer._states.items()
    }
    for uri, log in sequencer._log.items():
        channels[uri] = channels.get(uri, "") + json.dumps(list(log), sort_keys=True, default=str)
    return channels


def leaks(host: Host, *secrets: str) -> list[str]:
    """`channel: secret` for every secret found raw in any channel's state or log."""
    return [
        f"{uri}: {secret[:6]}..."
        for uri, text in all_channels(host).items()
        for secret in secrets
        if secret in text
    ]


async def connect(
    host: Host, *, headers: Mapping[str, str] | None = None, client_id: str = "c1"
) -> tuple[Wire, asyncio.Task[None]]:
    client, server = memory_pair()
    serving = asyncio.create_task(host.serve(server, headers=headers))
    wire = Wire(host, client)
    response = await wire.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["1.0.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    assert "result" in response, response
    wire.initialized = dict(response["result"])
    return wire, serving


async def open_session(
    wire: Wire,
    uri: str,
    provider: str,
    *,
    working_directories: Sequence[str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """A ready session; returns its default chat."""
    params: dict[str, Any] = {"channel": uri, "provider": provider}
    if working_directories is not None:
        params["workingDirectories"] = list(working_directories)
    if config is not None:
        params["config"] = dict(config)
    response = await wire.request("createSession", params)
    assert "error" not in response, response
    assert await wire.until(lambda: state(wire.host, uri).get("lifecycle") == "ready")
    await wire.request("subscribe", {"channel": uri})
    chat = state(wire.host, uri)["defaultChat"]
    await wire.request("subscribe", {"channel": chat})
    return str(chat)


async def start_turn(
    wire: Wire, chat: str, turn_id: str, text: str, *, meta: Mapping[str, Any] | None = None
) -> None:
    """Start a turn on *chat* without waiting for it; *meta* is the message's `_meta`."""
    message: dict[str, Any] = {"text": text, "origin": {"kind": "user"}}
    if meta is not None:
        message["_meta"] = dict(meta)
    await wire.dispatch(
        chat,
        {
            "type": "chat/turnStarted",
            "turnId": turn_id,
            "startedAt": "1970-01-01T00:00:01.000Z",
            "message": message,
        },
    )


async def wait_turn(wire: Wire, chat: str, turn_id: str) -> dict[str, Any]:
    """Wait for turn *turn_id* on *chat* to end; returns the finished turn."""

    def finished() -> dict[str, Any] | None:
        turns = state(wire.host, chat).get("turns", [])
        return next((t for t in turns if t.get("id") == turn_id), None)

    assert await wire.until(lambda: finished() is not None), state(wire.host, chat)
    turn = finished()
    assert turn is not None
    return turn


async def run_turn(wire: Wire, chat: str, turn_id: str, text: str) -> dict[str, Any]:
    """Drive one turn on *chat* to its end; returns the finished turn."""
    await start_turn(wire, chat, turn_id, text)
    return await wait_turn(wire, chat, turn_id)


async def cancel_turn(wire: Wire, chat: str, turn_id: str) -> None:
    await wire.dispatch(chat, {"type": "chat/turnCancelled", "turnId": turn_id})


def worker_chats(host: Host, session: str) -> list[dict[str, Any]]:
    """The session's worker chats (those a tool call opened)."""
    return [
        dict(chat)
        for chat in state(host, session).get("chats", [])
        if (chat.get("origin") or {}).get("kind") == "tool"
    ]


def notifications(turn: Mapping[str, Any]) -> list[str]:
    """The text of every system notification in *turn*."""
    return [
        str(part.get("content", ""))
        for part in found(turn, lambda m: m.get("kind") == "systemNotification")
    ]


def found(value: Any, wanted: Callable[[Mapping[str, Any]], bool]) -> list[Mapping[str, Any]]:
    """Every mapping anywhere in *value* that *wanted* accepts."""
    hits: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        if wanted(value):
            hits.append(value)
        for item in value.values():
            hits.extend(found(item, wanted))
    elif isinstance(value, list):
        for item in value:
            hits.extend(found(item, wanted))
    return hits


def text_of(turn: Mapping[str, Any]) -> str:
    return "".join(
        str(part.get("content", "")) for part in found(turn, lambda m: m.get("kind") == "markdown")
    )


async def shut(host: Host, wire: Wire, serving: asyncio.Task[None]) -> None:
    await wire.close()
    serving.cancel()
    await host.aclose()
