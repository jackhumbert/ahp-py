"""A host you can program, for testing a client.

**Public API.** The fastest way to lose an adopter is for them to be unable to
test their app, and two reference SDKs hid their in-memory transport in their own
test tree and heard about it.

It also simulates the awkward realities a real host produces and a naive fake
never does -- folded first deltas, sequence gaps, snapshot-only reconnects,
``-32601`` for unimplemented methods, ``-32008`` where the spec says ``-32001``.
Those are where client bugs actually live.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from typing import Any, Final

from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import Transport, memory_pair
from agent_host_protocol.types import JsonObject

__all__ = ["FakeHost", "FakeToolCall", "echo_host", "tool_call_host"]

Handler = Callable[[JsonObject], Any]


class FakeToolCall:
    """A tool call to weave into a scripted turn."""

    __slots__ = ("confirmed", "contributor", "name", "tool_call_id")

    def __init__(
        self,
        tool_call_id: str,
        name: str,
        *,
        confirmed: bool = False,
        contributor: JsonObject | None = None,
    ) -> None:
        self.tool_call_id = tool_call_id
        self.name = name
        #: When true the host asks before running, so the client has to answer.
        self.confirmed = confirmed
        #: ``{"kind": "client", "clientId": ...}`` makes the call the client's
        #: to execute.
        self.contributor = contributor


class FakeHost:
    """Drives one client over an in-memory transport pair."""

    def __init__(
        self,
        *,
        agents: list[JsonObject] | None = None,
        protocol_version: str = "0.7.0",
        server_seq: int = 1,
    ) -> None:
        self.protocol_version = protocol_version
        self.root_state: JsonObject = {"agents": agents or [], "activeSessions": 0}
        self.received: list[JsonObject] = []
        self._handlers: dict[str, Handler] = {}
        self._server_seq = server_seq
        self._client: Transport
        self._host: Transport
        self._client, self._host = memory_pair()
        self._pump: asyncio.Task[None] | None = None
        self._next_request_id = 1
        self._answers: dict[int, asyncio.Future[Any]] = {}

    # ── wiring ───────────────────────────────────────────────────────────────

    def transport(self) -> Transport:
        """The half a client connects to."""
        return self._client

    def on(self, method: str, handler: Handler) -> None:
        """Answer *method* with *handler*. Raise ``FakeRpcError`` to refuse."""
        self._handlers[method] = handler

    async def start(self) -> None:
        if self._pump is None:
            self._pump = asyncio.get_running_loop().create_task(self._serve(), name="fake-host")

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._pump
            self._pump = None
        with contextlib.suppress(Exception):
            await self._host.close()

    async def __aenter__(self) -> FakeHost:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ── pushing ──────────────────────────────────────────────────────────────

    def next_server_seq(self) -> int:
        seq = self._server_seq
        self._server_seq += 1
        return seq

    async def push(
        self,
        channel: str,
        action: Mapping[str, Any],
        *,
        origin: Mapping[str, Any] | None = None,
        rejection: str | None = None,
        server_seq: int | None = None,
    ) -> int:
        """Broadcast one ``ActionEnvelope``.

        *rejection* echoes a client's own action back refused, which is the only
        way a client learns its optimistic effect must be reverted.
        """
        seq = server_seq if server_seq is not None else self.next_server_seq()
        envelope: JsonObject = {"channel": channel, "action": dict(action), "serverSeq": seq}
        if origin is not None:
            envelope["origin"] = dict(origin)
        if rejection is not None:
            envelope["rejectionReason"] = rejection
        await self._send({"jsonrpc": "2.0", "method": "action", "params": envelope})
        return seq

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    async def call(self, method: str, params: Mapping[str, Any]) -> Any:
        """Issue a **host→client** request and await the client's answer.

        This is the direction no reference client implements, so a fake that
        cannot drive it cannot test the half of the protocol most likely to be
        wrong.
        """
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._answers[request_id] = future
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        )
        return await future

    async def emit_turn(
        self,
        chat: str,
        turn_id: str,
        *,
        text: str,
        tools: list[FakeToolCall] | None = None,
        fold_first_delta: bool = False,
    ) -> None:
        """Script a whole assistant turn.

        *fold_first_delta* reproduces the behaviour that breaks naive clients:
        a real host often folds a turn's opening text into the snapshot's
        ``activeTurn`` instead of emitting it as ``chat/delta``, so a client that
        renders only the deltas shows "ANANA" for "BANANA".
        """
        part_id = f"{turn_id}-part-0"
        # The fold: the opening character arrives inside the response part, and
        # only the remainder is ever emitted as a delta.
        folded, body = (text[:1], text[1:]) if fold_first_delta else ("", text)
        await self.push(chat, {"type": "chat/turnStarted", "turnId": turn_id})
        await self.push(
            chat,
            {
                "type": "chat/responsePart",
                "turnId": turn_id,
                "part": {"id": part_id, "kind": "markdown", "content": folded},
            },
        )
        for chunk in body:
            await self.push(
                chat,
                {"type": "chat/delta", "turnId": turn_id, "partId": part_id, "content": chunk},
            )
        for tool in tools or []:
            await self._emit_tool(chat, turn_id, tool)
        await self.push(chat, {"type": "chat/turnComplete", "turnId": turn_id})

    async def _emit_tool(self, chat: str, turn_id: str, tool: FakeToolCall) -> None:
        start: JsonObject = {
            "type": "chat/toolCallStart",
            "turnId": turn_id,
            "toolCallId": tool.tool_call_id,
            "toolName": tool.name,
        }
        if tool.contributor is not None:
            start["contributor"] = dict(tool.contributor)
        await self.push(chat, start)
        if tool.confirmed:
            await self.push(
                chat,
                {
                    "type": "chat/toolCallReady",
                    "turnId": turn_id,
                    "toolCallId": tool.tool_call_id,
                },
            )

    # ── the loop ─────────────────────────────────────────────────────────────

    async def _send(self, message: Mapping[str, Any]) -> None:
        await self._host.send(dict(message))

    async def _serve(self) -> None:
        while True:
            message = await self._host.receive()
            if message is None:
                return
            self.received.append(dict(message))
            if "id" in message and "method" not in message:
                self._resolve(message)
                continue
            if "method" not in message:
                continue
            await self._handle(message)

    def _resolve(self, message: Mapping[str, Any]) -> None:
        future = self._answers.pop(message.get("id"), None)  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if "error" in message:
            future.set_exception(FakeRpcError(message["error"]))
        else:
            future.set_result(message.get("result"))

    async def _handle(self, message: Mapping[str, Any]) -> None:
        method = str(message.get("method"))
        raw = message.get("params")
        params: JsonObject = dict(raw) if isinstance(raw, Mapping) else {}
        if "id" not in message:
            return  # a notification; `received` already has it

        request_id = message["id"]
        handler = self._handlers.get(method)
        if handler is None:
            # An unregistered method answers -32601, which is what a real host
            # does for anything it declines: AHP has no capability object, so
            # this error IS the "no". A client must treat it as an answer.
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
            return
        try:
            result = handler(params)
            if asyncio.iscoroutine(result):
                result = await result
        except FakeRpcError as exc:
            await self._send({"jsonrpc": "2.0", "id": request_id, "error": exc.error})
            return
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})


class FakeRpcError(Exception):
    """Raise from a handler to answer with a JSON-RPC error."""

    def __init__(self, error: Mapping[str, Any]) -> None:
        super().__init__(str(error))
        self.error: Final = dict(error)


def _default_initialize(host: FakeHost, params: JsonObject) -> JsonObject:
    # Answers with whatever it was configured to speak, even when that is not
    # among the offered versions -- which is exactly the reference client's bug
    # and is what `verify_negotiated_version` exists to catch.
    result: JsonObject = {
        "protocolVersion": host.protocol_version,
        "serverSeq": host._server_seq,
        "serverInfo": {"name": "FakeHost", "version": "0"},
        "snapshots": [],
    }
    requested = params.get("initialSubscriptions") or []
    for uri in requested:
        if uri == ROOT_URI:
            result["snapshots"].append(
                {"resource": ROOT_URI, "state": host.root_state, "fromSeq": host._server_seq}
            )
    return result


def _install_defaults(host: FakeHost) -> None:
    host.on("initialize", lambda params: _default_initialize(host, params))
    host.on("ping", lambda _params: {})
    host.on("listSessions", lambda _params: {"items": []})
    host.on(
        "subscribe",
        lambda params: {
            "snapshot": {
                "resource": params.get("channel", ""),
                "state": host.root_state if params.get("channel") == ROOT_URI else {},
                "fromSeq": host._server_seq,
            }
        },
    )


def echo_host(**kwargs: Any) -> FakeHost:
    """A host that completes a handshake and answers the basics."""
    host = FakeHost(agents=[{"id": "echo", "displayName": "Echo"}], **kwargs)
    _install_defaults(host)
    return host


def tool_call_host(*, confirmed: bool = False, client_tool: str | None = None) -> FakeHost:
    """An echo host whose scripted turn contains a tool call.

    *client_tool* marks the call ``contributor: {"kind": "client"}``, so the
    client is expected to execute it and report back.
    """
    host = echo_host()
    host.pending_tool = FakeToolCall(  # type: ignore[attr-defined]
        "tool-1",
        client_tool or "read_file",
        confirmed=confirmed,
        contributor={"kind": "client"} if client_tool else None,
    )
    return host
