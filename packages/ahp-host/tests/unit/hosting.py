"""A host, one client, and a session: what the provider-API tests drive.

Not a test module. The provider extension points are only worth testing
through a real host doing the thing -- a frame that looks right in isolation
is the class of defect this repository keeps finding -- and each of those
tests needs the same few steps to get there. A background reader collects
every frame, so a test waits on a *condition* rather than sleeping.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair
from ahp_protocol.transport.memory import MemoryTransport

from ahp_host.core import Host
from tests.conformance.schemas import assert_valid_action, assert_valid_state


class Wire:
    """One client connection, recording everything the host sends it."""

    def __init__(self, host: Host, transport: MemoryTransport) -> None:
        self.host = host
        self._transport = transport
        self.frames: list[dict[str, Any]] = []
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._client_seq = 0
        #: The `initialize` result, once `connect` has had it.
        self.initialized: dict[str, Any] = {}
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        while True:
            message = await self._transport.receive()
            if message is None:
                return
            request_id = message.get("id")
            if "method" not in message and isinstance(request_id, int):
                waiter = self._waiting.pop(request_id, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(dict(message))
                    continue
            self.frames.append(dict(message))

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Send a request and return its whole response frame."""
        self._next_id += 1
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[self._next_id] = waiter
        await self._transport.send(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": dict(params)}
        )
        return await asyncio.wait_for(waiter, timeout=5)

    async def dispatch(self, channel: str, action: Mapping[str, Any]) -> int:
        """`dispatchAction`; returns the `clientSeq` it carried."""
        self._client_seq += 1
        await self._transport.send(
            {
                "jsonrpc": "2.0",
                "method": "dispatchAction",
                "params": {"channel": channel, "clientSeq": self._client_seq, "action": action},
            }
        )
        return self._client_seq

    def envelopes(self, channel: str | None = None) -> list[dict[str, Any]]:
        """Every action envelope received, optionally for one channel."""
        return [
            frame["params"]
            for frame in self.frames
            if frame.get("method") == "action"
            and (channel is None or frame["params"].get("channel") == channel)
        ]

    def actions(self, channel: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        """The actions inside those envelopes, optionally of one type."""
        return [
            envelope["action"]
            for envelope in self.envelopes(channel)
            if kind is None or envelope["action"].get("type") == kind
        ]

    def echo(self, channel: str, client_seq: int) -> dict[str, Any] | None:
        """The envelope the host sent back for one of our dispatches."""
        return next(
            (
                e
                for e in self.envelopes(channel)
                if (e.get("origin") or {}).get("clientSeq") == client_seq
            ),
            None,
        )

    async def until(self, ready: Callable[[], bool], *, timeout: float = 5.0) -> bool:
        """Wait for *ready* to hold; returns whether it did.

        Returns rather than raising, so the caller's assertion is the message.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while not ready():
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def echoed(self, channel: str, client_seq: int) -> dict[str, Any]:
        """Wait for the echo of one dispatch, and return it."""
        await self.until(lambda: self.echo(channel, client_seq) is not None)
        echo = self.echo(channel, client_seq)
        assert echo is not None, f"no echo for clientSeq {client_seq} on {channel}"
        return echo

    async def close(self) -> None:
        self._reader.cancel()


def state(host: Host, uri: str) -> dict[str, Any]:
    """A channel's reduced state, or ``{}``."""
    found = host.sequencer.state_of(uri)
    return dict(found) if isinstance(found, Mapping) else {}


async def connect(host: Host, client_id: str = "c1") -> tuple[Wire, asyncio.Task[None]]:
    """An initialized client on *host*, and the task serving it."""
    client, server = memory_pair()
    serving = asyncio.create_task(host.serve(server))
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


async def open_session(wire: Wire, uri: str, **params: Any) -> str:
    """Create a session, wait for it to be ready, subscribe; return its default chat."""
    response = await wire.request("createSession", {"channel": uri, "provider": "echo", **params})
    assert "error" not in response, response
    ready = await wire.until(lambda: state(wire.host, uri).get("lifecycle") == "ready")
    assert ready, state(wire.host, uri)
    await wire.request("subscribe", {"channel": uri})
    chat = state(wire.host, uri)["defaultChat"]
    await wire.request("subscribe", {"channel": chat})
    assert isinstance(chat, str)
    return chat


def turn_started(turn_id: str, text: str = "hello", **message: Any) -> dict[str, Any]:
    """A client's `chat/turnStarted`."""
    return {
        "type": "chat/turnStarted",
        "turnId": turn_id,
        "startedAt": "1970-01-01T00:00:01.000Z",
        "message": {"text": text, "origin": {"kind": "user"}, **message},
    }


def finished(host: Host, chat: str, turn_id: str) -> bool:
    """Whether *turn_id* has settled into *chat*'s transcript."""
    return any(t.get("id") == turn_id for t in state(host, chat).get("turns", []))


async def run_turn(wire: Wire, chat: str, turn_id: str, text: str = "hello") -> None:
    """Drive one turn on *chat* to its end."""
    await wire.dispatch(chat, turn_started(turn_id, text))
    done = await wire.until(lambda: finished(wire.host, chat, turn_id))
    assert done, state(wire.host, chat)


async def shut(host: Host, wire: Wire, serving: asyncio.Task[None]) -> None:
    await wire.close()
    serving.cancel()
    await host.aclose()


def assert_frames_valid(wire: Wire, *states: tuple[str, str]) -> None:
    """Every accepted action *wire* received matches the vendored schema.

    *states* are ``(channel kind, uri)`` pairs whose reduced state is checked
    too. A rejected echo is skipped: it is the client's own frame, returned.
    """
    for envelope in wire.envelopes():
        if "rejectionReason" not in envelope:
            assert_valid_action(envelope["action"])
    for kind, uri in states:
        assert_valid_state(kind, state(wire.host, uri))
