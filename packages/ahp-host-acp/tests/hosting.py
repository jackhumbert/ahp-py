"""A real `Host` serving this provider, and one in-memory client: not a test module.

The provider's surfaces are only proven through the host doing the thing --
`createChat` reaching `chat_opened`, a turn routed by its chat, a `fileEdit`
the host stored. A background reader keeps every frame, so a test waits on a
condition rather than sleeping.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
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
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        while (message := await self._transport.receive()) is not None:
            request_id = message.get("id")
            if "method" in message and request_id is not None:
                await self._transport.send(
                    {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32008, "message": "no"}}
                )
            elif isinstance(request_id, int) and request_id in self._waiting:
                self._waiting.pop(request_id).set_result(dict(message))
            else:
                self.frames.append(dict(message))

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


def state(host: Host, uri: str) -> dict[str, Any]:
    found = host.sequencer.state_of(uri)
    return dict(found) if isinstance(found, Mapping) else {}


async def connect(host: Host) -> tuple[Wire, asyncio.Task[None]]:
    client, server = memory_pair()
    serving = asyncio.create_task(host.serve(server))
    wire = Wire(host, client)
    response = await wire.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "c1",
            "protocolVersions": ["1.0.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    assert "result" in response, response
    wire.initialized = dict(response["result"])
    return wire, serving


async def open_session(wire: Wire, uri: str, provider: str) -> str:
    """A ready session; returns its default chat."""
    response = await wire.request("createSession", {"channel": uri, "provider": provider})
    assert "error" not in response, response
    assert await wire.until(lambda: state(wire.host, uri).get("lifecycle") == "ready")
    await wire.request("subscribe", {"channel": uri})
    chat = state(wire.host, uri)["defaultChat"]
    await wire.request("subscribe", {"channel": chat})
    return str(chat)


async def run_turn(wire: Wire, chat: str, turn_id: str, text: str) -> dict[str, Any]:
    """Drive one turn on *chat* to its end; returns the finished turn."""
    await wire.dispatch(
        chat,
        {
            "type": "chat/turnStarted",
            "turnId": turn_id,
            "startedAt": "1970-01-01T00:00:01.000Z",
            "message": {"text": text, "origin": {"kind": "user"}},
        },
    )

    def finished() -> dict[str, Any] | None:
        turns = state(wire.host, chat).get("turns", [])
        return next((t for t in turns if t.get("id") == turn_id), None)

    assert await wire.until(lambda: finished() is not None), state(wire.host, chat)
    turn = finished()
    assert turn is not None
    return turn


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
