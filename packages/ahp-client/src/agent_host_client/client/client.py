"""The single-shot JSON-RPC client.

One transport, one lifecycle. When the transport dies this client is finished --
recovery means a new transport and therefore a new client, which is why every
reconnect decision lives in ``hosts/`` and none of it lives here (ADR 0003).

Behavioural parity with the reference TypeScript client, **including its
quirks**, except where an ADR names the divergence. The quirks are load-bearing:
a host was written against them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from types import EllipsisType, TracebackType
from typing import Any, Final, Protocol, Self

from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import Transport, TransportClosed
from agent_host_protocol.types import JsonObject
from agent_host_protocol.versions import DEFAULT_SUPPORTED_VERSIONS, parse_version

from agent_host_client.client.commands import CommandsMixin
from agent_host_client.client.errors import (
    ClientClosed,
    ProtocolVersionError,
    RequestTimeout,
    RpcError,
    TransportError,
    rpc_error_from,
)
from agent_host_client.client.events import (
    NOTIFICATION_METHODS,
    ActionEvent,
    AuthRequiredEvent,
    ClientEvent,
    ConnectionState,
    Diagnostic,
    DroppedEvents,
    MalformedFrame,
    OtlpEvent,
    ProgressEvent,
    SessionAdded,
    SessionRemoved,
    SessionSummaryChanged,
    SubscriptionEvent,
    UnknownResponse,
)
from agent_host_client.client.mirror import StateMirror
from agent_host_client.client.queue import BroadcastQueue, BroadcastReader

__all__ = [
    "AhpClient",
    "ClientConfig",
    "DispatchHandle",
    "ServerRequestHandler",
    "Subscription",
]

#: Past this many undecodable frames the peer is not recovering, and holding the
#: socket open only delays the caller's timeouts. VS Code closes with 4002.
MALFORMED_FRAME_LIMIT: Final = 8


class ServerRequestHandler(Protocol):
    """Answers a host-initiated request.

    Implemented by ``serve/``; ``client`` only ever defines it. Raise an
    :class:`RpcError` to send a specific error back; anything else becomes
    ``-32603``.
    """

    async def __call__(self, method: str, params: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class ClientConfig:
    request_timeout: float | None = 30.0
    #: ``0`` is unbounded, and is the default for per-channel subscriptions on
    #: purpose: a dropped ``ActionEnvelope`` desyncs the mirror permanently, so
    #: dropping one to bound memory is never the right trade (ADR 0002).
    subscription_buffer: int = 0
    #: Fan-in taps *are* bounded -- they are observation, not state.
    event_buffer: int = 4096
    #: What the vendored tables actually cover, not what upstream declares.
    #: Offering a version whose tables we lack means negotiating a protocol we
    #: cannot reduce (ADR 0005).
    protocol_versions: tuple[str, ...] = DEFAULT_SUPPORTED_VERSIONS
    verify_negotiated_version: bool = True
    client_info: JsonObject | None = None
    capabilities: JsonObject | None = None
    locale: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchHandle:
    """What a fire-and-forget dispatch gives you back: the number to reconcile on."""

    client_seq: int


class Subscription:
    """A reader over one channel's event stream.

    Closing this ends *this consumer's* iteration. It does not release the
    server-side subscription -- that is :meth:`AhpClient.unsubscribe`, and
    conflating them would let one consumer silently blind another.
    """

    __slots__ = ("_reader", "uri")

    def __init__(self, uri: str, reader: BroadcastReader[SubscriptionEvent]) -> None:
        self.uri: Final = uri
        self._reader = reader

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> SubscriptionEvent:
        return await self._reader.__anext__()

    async def aclose(self) -> None:
        await self._reader.aclose()


class AhpClient(CommandsMixin):
    """Transport-agnostic AHP client.

    **Concurrency.** One reader task drains the transport; one writer task
    drains an outbound queue. The writer exists because two concurrent
    ``await transport.send()`` calls can interleave frames.

    ``asyncio.TaskGroup`` was evaluated for the pair and does not fit: a
    ``TaskGroup`` body blocks until its children finish, and ``connect()`` must
    return while they keep running. Driving one through bare
    ``__aenter__``/``__aexit__`` to span that gap gives up the exception
    propagation that is the reason to want it. So the tasks are explicit, held
    in a set against garbage collection, with done-callbacks that surface
    failures onto the connection state.
    """

    def __init__(
        self,
        transport: Transport,
        config: ClientConfig | None = None,
        *,
        first_request_id: int = 1,
        first_client_seq: int = 1,
    ) -> None:
        """*first_request_id* and *first_client_seq* seed the counters.

        One client is one transport, so "ids never reset across transport
        swaps" is a promise only the supervisor above can keep: it constructs a
        fresh client per reconnect and seeds it from where the predecessor
        stopped (:attr:`next_request_id`, :attr:`next_client_seq`). VS Code's
        first frame on a fresh socket carried id 66 -- a peer must not assume
        per-connection numbering.
        """
        self._transport = transport
        self._config = config or ClientConfig()
        self._state = ConnectionState("idle")

        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_request_id = first_request_id
        self._next_client_seq = first_client_seq

        self._subscriptions: dict[str, BroadcastQueue[SubscriptionEvent]] = {}
        self._events: BroadcastQueue[ClientEvent] = BroadcastQueue(
            self._config.event_buffer,
            on_drop=lambda n: self._diagnose(DroppedEvents("events", n)),
        )
        self._states: BroadcastQueue[ConnectionState] = BroadcastQueue(64)
        self._diagnostics: BroadcastQueue[Diagnostic] = BroadcastQueue(1024)

        self._outbox: asyncio.Queue[JsonObject | None] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()
        self._handler: ServerRequestHandler | None = None
        self._malformed = 0
        self._last_seen_server_seq = 0
        self._mirror: StateMirror | None = None

    # ── observation ──────────────────────────────────────────────────────────

    @property
    def connection_state(self) -> ConnectionState:
        return self._state

    @property
    def last_seen_server_seq(self) -> int:
        """Highest ``serverSeq`` observed. What ``reconnect`` resumes from."""
        return self._last_seen_server_seq

    @property
    def next_request_id(self) -> int:
        """Where the id counter stands. Seeds a successor's ``first_request_id``."""
        return self._next_request_id

    @property
    def next_client_seq(self) -> int:
        """Where the seq counter stands. Seeds a successor's ``first_client_seq``."""
        return self._next_client_seq

    def state_changes(self) -> BroadcastReader[ConnectionState]:
        return self._states.reader()

    def events(self) -> BroadcastReader[ClientEvent]:
        """Every inbound event, every channel, tagged. Bounded; drops are reported."""
        return self._events.reader()

    def diagnostics(self) -> BroadcastReader[Diagnostic]:
        """Recoverable faults: gaps, drops, rejections, malformed frames."""
        return self._diagnostics.reader()

    def set_state_mirror(self, mirror: StateMirror | None) -> None:
        """Feed a mirror on the **read path**, before any fan-out.

        This is what makes bounding the event taps safe (ADR 0002): state is
        applied before a single consumer is notified, so a consumer that has
        stopped draining -- or was never attached -- costs itself events and
        never costs anyone correctness. Every other client fans out first and
        lets consumers update mirrors afterwards, which is why their own docs
        admit a dropped envelope desyncs state permanently.

        Mutation happens from more than one task: this path, and
        `record_pending` from whichever task called `dispatch`. That is safe
        because **no mutation spans an await**, which is a weaker and more
        precise rule than "one task" -- and the rule to preserve. Never reduce
        in a thread pool.
        """
        self._mirror = mirror

    def set_server_request_handler(self, handler: ServerRequestHandler | None) -> None:
        """Install the answer to host-initiated requests.

        With none installed every inbound request is answered ``-32601``, so the
        host never leaks a pending request waiting on us.
        """
        self._handler = handler

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Start the reader and writer. Idempotent; does not handshake."""
        if self._state.status != "idle":
            return
        self._set_state(ConnectionState("connected"))
        self._spawn(self._read_loop(), "ahp-client-read")
        self._spawn(self._write_loop(), "ahp-client-write")

    async def shutdown(self) -> None:
        """Close cleanly. Idempotent.

        Tear-down happens **before** the transport closes, so an in-flight
        request raises :class:`ClientClosed` rather than racing the read loop to
        a :class:`TransportError`. Callers key retry decisions on that
        difference.

        A connection the *read loop* ended is already ``closed``, and only the
        state transition is redundant then -- the tasks are not. Skipping the
        whole body left ``_write_loop`` parked on ``await self._outbox.get()``
        forever, holding this client and its transport, once per reconnect;
        asyncio's destruction warning then fires at an unrelated moment with a
        stack pointing nowhere useful.
        """
        if self._state.status not in {"closing", "closed"}:
            self._set_state(ConnectionState("closing"))
            self._tear_down(None)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await self._transport.close()
        self._outbox.put_nowait(None)
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.shutdown()

    # ── handshake ────────────────────────────────────────────────────────────

    async def initialize(
        self,
        *,
        client_id: str,
        protocol_versions: Sequence[str] | None = None,
        initial_subscriptions: Sequence[str] | None = None,
        client_info: Mapping[str, Any] | None = None,
        capabilities: Mapping[str, Any] | None = None,
        locale: str | None = None,
    ) -> JsonObject:
        """The opening handshake.

        Sends ``clientInfo`` and ``capabilities`` where the reference helper
        cannot -- its signature omits both despite the types supporting them.
        Absent keys are omitted, never sent as ``null``: ``json.dumps`` writes
        ``null`` where ``JSON.stringify`` drops the key, and the difference
        between ``capabilities: {"mcpApps": {}}`` and ``capabilities: null`` is
        load-bearing.
        """
        offered = tuple(protocol_versions or self._config.protocol_versions)
        params: JsonObject = {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": list(offered),
        }
        if initial_subscriptions:
            params["initialSubscriptions"] = list(initial_subscriptions)
        info = client_info if client_info is not None else self._config.client_info
        if info is not None:
            params["clientInfo"] = dict(info)
        caps = capabilities if capabilities is not None else self._config.capabilities
        if caps is not None:
            params["capabilities"] = dict(caps)
        chosen_locale = locale if locale is not None else self._config.locale
        if chosen_locale is not None:
            params["locale"] = chosen_locale

        raw = await self.request("initialize", params)
        result: JsonObject = raw if isinstance(raw, dict) else {}
        self._absorb_server_seq(result.get("serverSeq"))
        if self._config.verify_negotiated_version:
            self._verify_version(result.get("protocolVersion"), offered)
        return result

    async def reconnect(
        self,
        *,
        client_id: str,
        last_seen_server_seq: int,
        subscriptions: Sequence[str],
    ) -> JsonObject:
        """Resume a dropped connection.

        A valid **first** request on a fresh transport -- there is no prior
        ``initialize`` to precede it. ``channel`` is sent because
        ``ReconnectParams`` extends ``BaseParams`` and the spec's own worked
        example includes it; VS Code omits it, and is out of spec there.
        """
        params: JsonObject = {
            "channel": ROOT_URI,
            "clientId": client_id,
            "lastSeenServerSeq": last_seen_server_seq,
            "subscriptions": list(subscriptions),
        }
        raw = await self.request("reconnect", params)
        return raw if isinstance(raw, dict) else {}

    def _verify_version(self, negotiated: Any, offered: Sequence[str]) -> None:
        if not isinstance(negotiated, str) or parse_version(negotiated) is None:
            raise ProtocolVersionError(str(negotiated), offered)
        if negotiated not in offered:
            raise ProtocolVersionError(negotiated, offered)

    # ── subscriptions ────────────────────────────────────────────────────────

    async def subscribe(
        self,
        uri: str,
        *,
        delivery: Mapping[str, Any] | None = None,
        view: Mapping[str, Any] | None = None,
    ) -> tuple[JsonObject, Subscription]:
        """Subscribe and return ``(result, subscription)``.

        The local queue is attached **before** the request goes out, so nothing
        delivered during the round trip is lost, and is rolled back if the
        request fails. (Rust leaks the queue here; Go and Swift roll back.)
        """
        # Whether *this* call created the queue decides how far the rollback
        # goes: tearing down a queue another consumer is already reading would
        # silently blind them because our request was refused.
        existed = uri in self._subscriptions
        subscription = self.attach_subscription(uri)
        params: JsonObject = {"channel": uri}
        if delivery is not None:
            params["delivery"] = dict(delivery)
        if view is not None:
            params["view"] = dict(view)
        try:
            raw = await self.request("subscribe", params)
            result: JsonObject = raw if isinstance(raw, dict) else {}
        except BaseException:
            await subscription.aclose()
            if not existed:
                queue = self._subscriptions.pop(uri, None)
                if queue is not None:
                    queue.close()
            raise
        return result, subscription

    def attach_subscription(self, uri: str) -> Subscription:
        """A local reader with no wire traffic.

        For a URI already covered by ``initialSubscriptions``, or for a second
        consumer of one that is already subscribed.
        """
        self._assert_open()
        queue = self._subscriptions.get(uri)
        if queue is None:
            queue = BroadcastQueue(self._config.subscription_buffer)
            self._subscriptions[uri] = queue
        return Subscription(uri, queue.reader())

    async def unsubscribe(self, uri: str) -> None:
        """Release the server-side subscription and drop the local fan-out.

        A **no-op after shutdown**, unlike every other method, which raise. The
        reference client makes the same distinction and it is the right one:
        unsubscribing from a closed client is a caller tidying up, not a
        mistake.
        """
        if self._state.status in {"closing", "closed"}:
            return
        queue = self._subscriptions.pop(uri, None)
        if queue is not None:
            queue.close()
        self.notify("unsubscribe", {"channel": uri})

    # ── dispatch ─────────────────────────────────────────────────────────────

    def dispatch(
        self,
        channel: str,
        action: Mapping[str, Any],
        client_seq: int | None = None,
    ) -> DispatchHandle:
        """Fire a write-ahead ``dispatchAction``.

        **Synchronous, deliberately.** If this were ``async def``, two
        coroutines could interleave between allocating ``clientSeq`` and
        enqueueing, putting 5 on the wire before 4 -- and the host's echo would
        then reconcile against an order the client never sent. TypeScript gets
        this free from single-threaded JS; asyncio does not.
        """
        self._assert_open()
        seq = self._next_client_seq if client_seq is None else client_seq
        # An explicit seq still advances the counter, so a caller mixing both
        # forms cannot collide with itself.
        self._next_client_seq = max(self._next_client_seq, seq + 1)
        if self._mirror is not None:
            # Optimistic apply happens before the frame is enqueued, so a UI
            # reading the mirror never observes a window where the action has
            # been sent but not yet reflected.
            self._mirror.record_pending(channel, action, seq)
        self.notify("dispatchAction", {"channel": channel, "clientSeq": seq, "action": action})
        return DispatchHandle(seq)

    def redispatch(self, channel: str, action: Mapping[str, Any], client_seq: int) -> None:
        """Put an already-pending dispatch back on the wire after a reconnect.

        Deliberately **not** :meth:`dispatch`: the mirror already holds a
        pending entry for this ``clientSeq`` from the original send, and
        recording a second would replay the action twice into ``optimistic``.
        The original number goes back out unchanged, so the host's echo retires
        the entry that is actually there.

        Advancing ``_next_client_seq`` past it is what stops a collision: the
        fresh client of a reconnect starts counting at 1, so without this a
        re-sent entry 3 and the caller's next new dispatch both claim 3 and the
        first echo retires the wrong one.
        """
        self._assert_open()
        self._next_client_seq = max(self._next_client_seq, client_seq + 1)
        self.notify(
            "dispatchAction", {"channel": channel, "clientSeq": client_seq, "action": action}
        )

    async def ping(self) -> None:
        """Liveness. Answered whether or not we have completed ``initialize``."""
        await self.request("ping", {"channel": ROOT_URI})

    # ── raw JSON-RPC ─────────────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | EllipsisType | None = ...,
    ) -> Any:
        """Send a request and await its result.

        *timeout* defaults to the configured one; pass ``None`` to disable it for
        this call. ``...`` is the "not supplied" sentinel because ``None`` is a
        meaningful value here.
        """
        self._assert_open()
        deadline = self._config.request_timeout if isinstance(timeout, EllipsisType) else timeout
        request_id = self._next_request_id
        # Monotonic for this client's whole life; continuity across transport
        # swaps is the supervisor's, via the `first_request_id` seed. VS Code's
        # first frame on a fresh socket carried id 66, so a peer must not
        # assume per-connection numbering.
        self._next_request_id += 1

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._outbox.put_nowait(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        )
        try:
            if deadline is not None and deadline > 0:
                try:
                    return await asyncio.wait_for(asyncio.shield(future), deadline)
                except asyncio.CancelledError:
                    # A caller's cancellation stops at the shield: the inner
                    # future stays pending, the finally's guard sees neither
                    # `cancelled()` nor `done()`, and the entry would leak until
                    # tear-down. Propagate the cancellation through the shield
                    # so the one cleanup path below owns every exit.
                    future.cancel()
                    raise
            return await future
        except TimeoutError:
            # Whoever pops owns the settle, so a late response cannot also fire.
            if self._pending.pop(request_id, None) is not None:
                raise RequestTimeout(method, deadline or 0.0) from None
            return future.result()
        finally:
            # A cancelled caller must not leave a resolvable entry behind, or a
            # late response resolves a future nobody retrieves and asyncio logs
            # it as an unhandled exception.
            if future.cancelled() or (future.done() and self._pending.get(request_id) is future):
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        """Fire-and-forget. Synchronous, for the reason given on :meth:`dispatch`."""
        self._assert_open()
        self._outbox.put_nowait({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    # ── internals ────────────────────────────────────────────────────────────

    def _assert_open(self) -> None:
        if self._state.status in {"closing", "closed"}:
            raise ClientClosed()

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._reap)

    def _reap(self, task: asyncio.Task[None]) -> None:
        """Surface a dead task's failure onto the connection state.

        A discard-only callback here is the silent-hang generator: the reader
        dies, its exception sits unretrieved, `_state` stays "connected", and
        every request waits out its full timeout against a connection nobody is
        reading. Cancellation is the one expected way for these tasks to end.
        """
        self._tasks.discard(task)
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            self._tear_down(
                TransportError("protocol", f"task {task.get_name()!r} failed: {failure!r}")
            )

    def _set_state(self, state: ConnectionState) -> None:
        self._state = state
        self._states.publish(state)

    def _diagnose(self, diagnostic: Diagnostic) -> None:
        self._diagnostics.publish(diagnostic)

    def _absorb_server_seq(self, raw: Any) -> None:
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > self._last_seen_server_seq:
            self._last_seen_server_seq = raw

    def _tear_down(self, error: BaseException | None) -> None:
        if self._state.status == "closed":
            return
        self._set_state(ConnectionState("closed", error))
        failure = error if error is not None else ClientClosed()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._pending.clear()
        for queue in self._subscriptions.values():
            queue.close()
        self._subscriptions.clear()
        self._events.close()
        self._diagnostics.close()
        self._states.close()

    async def _write_loop(self) -> None:
        while True:
            message = await self._outbox.get()
            if message is None:
                return
            try:
                await self._transport.send(message)
            except (TransportClosed, OSError) as exc:
                self._tear_down(TransportError("io", f"send failed: {exc}"))
                return

    async def _read_loop(self) -> None:
        try:
            while self._state.status == "connected":
                try:
                    message = await self._transport.receive()
                except json.JSONDecodeError as exc:
                    # A transport that parses eagerly raises here per bad frame;
                    # treat it exactly as an inline malformed frame (invariant
                    # 8: log and continue). Caught *inside* the loop, because
                    # letting it end the coroutine would leave the state
                    # "connected" with nobody reading -- a silent hang.
                    self._on_malformed(str(exc))
                    continue
                if message is None:
                    self._tear_down(TransportError("closed", "transport closed"))
                    return
                try:
                    self._on_message(message)
                except Exception as exc:  # one bad frame, not the whole loop
                    # Nothing below is *supposed* to raise, but "supposed to"
                    # held the reader's life on reducer totality and the mirror's
                    # thread assert. One bad envelope becomes a counted
                    # diagnostic under the malformed-frame policy instead of
                    # ending the loop with the state stuck "connected".
                    self._on_malformed(f"unhandled error processing frame: {exc!r}")
        except asyncio.CancelledError:
            raise
        except TransportError as exc:
            self._tear_down(exc)
        except (TransportClosed, OSError) as exc:
            self._tear_down(TransportError("io", f"receive failed: {exc}"))

    def _on_malformed(self, detail: str) -> None:
        """One bad frame must not kill unrelated in-flight requests.

        Report and continue -- their timeouts will fire honestly. Past the limit
        the peer is not recovering and holding the socket open only delays them.
        """
        self._malformed += 1
        self._diagnose(MalformedFrame(detail, self._malformed))
        if self._malformed >= MALFORMED_FRAME_LIMIT:
            self._tear_down(
                TransportError("protocol", f"{self._malformed} malformed frames; giving up")
            )
            # Tear-down settles futures but does not touch the socket, and a
            # bare AhpClient user has no supervisor to close it -- the peer
            # would be held open, streaming garbage, until GC. VS Code closes
            # here with 4002 "malformed-frames".
            self._spawn(self._close_transport(), "ahp-client-close")

    async def _close_transport(self) -> None:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await self._transport.close()

    def _on_message(self, message: Mapping[str, Any]) -> None:
        """Demux structurally, not on a type tag -- JSON-RPC has none."""
        if not isinstance(message, Mapping):
            self._on_malformed(f"expected an object, got {type(message).__name__}")
            return
        has_id = "id" in message
        if has_id and "result" in message:
            self._settle(message["id"], message["result"], None)
        elif has_id and "error" in message:
            error = message["error"]
            self._settle(
                message["id"],
                None,
                rpc_error_from(error if isinstance(error, Mapping) else {}),
            )
        elif has_id and "method" in message:
            self._spawn(self._answer(message), "ahp-client-inbound")
        elif "method" in message:
            self._on_notification(message)
        else:
            self._on_malformed(f"neither a request, a response nor a notification: {message!r}")

    def _settle(self, request_id: Any, result: Any, error: RpcError | None) -> None:
        # `bool` is an `int` and `True == 1` as a dict key, so without the
        # exclusion a frame with `"id": true` settles request 1 with the wrong
        # payload. Same discipline as `_absorb_server_seq` and `rpc_error_from`.
        is_valid_id = isinstance(request_id, int) and not isinstance(request_id, bool)
        future = self._pending.pop(request_id, None) if is_valid_id else None
        if future is None:
            self._diagnose(UnknownResponse(request_id))
            return
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    async def _answer(self, request: Mapping[str, Any]) -> None:
        """Answer one host-initiated request.

        Runs as its own task: a handler may re-enter this client (a resource
        server reading a `ContentRef` calls back out), and answering inline
        would deadlock the read loop against itself.
        """
        request_id = request.get("id")
        method = str(request.get("method", ""))
        params = request.get("params")
        params = params if isinstance(params, Mapping) else {}
        response: JsonObject = {"jsonrpc": "2.0", "id": request_id}
        if self._handler is None:
            response["error"] = {
                "code": -32601,
                "message": f'no handler for server method "{method}"',
            }
        else:
            try:
                response["result"] = await self._handler(method, params)
            except RpcError as exc:
                error: JsonObject = {"code": exc.code, "message": exc.message}
                if exc.data is not None:
                    error["data"] = exc.data
                response["error"] = error
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a handler fault is a -32603, not a crash
                response["error"] = {"code": -32603, "message": str(exc)}
        if self._state.status == "connected":
            self._outbox.put_nowait(response)

    def _on_notification(self, message: Mapping[str, Any]) -> None:
        """Fan a notification out to its channel, and to the global tap.

        Nine methods, where the reference client handles five and drops the rest
        at a ``default:`` branch that reaches neither subscriptions nor
        ``events()`` -- so ``root/progress`` and the three ``otlp/*`` signals are
        unreachable there despite the source comment claiming otherwise.

        An exception must never escape: there is no response to carry it, and an
        escaping one ends the read loop, dropping the connection over one bad
        frame from an untrusted peer.
        """
        method = str(message.get("method", ""))
        raw = message.get("params")
        params: JsonObject = dict(raw) if isinstance(raw, Mapping) else {}
        if method not in NOTIFICATION_METHODS:
            return

        event: SubscriptionEvent
        if method == "action":
            event = ActionEvent(params)
            self._absorb_server_seq(params.get("serverSeq"))
            if self._mirror is not None:
                self._mirror.apply(params)
        elif method == "root/sessionAdded":
            event = SessionAdded(params)
        elif method == "root/sessionRemoved":
            event = SessionRemoved(params)
        elif method == "root/sessionSummaryChanged":
            event = SessionSummaryChanged(params)
        elif method == "auth/required":
            event = AuthRequiredEvent(params)
        elif method == "root/progress":
            event = ProgressEvent(params)
        else:
            signal = method.removeprefix("otlp/export").lower()
            if signal not in {"logs", "traces", "metrics"}:
                return
            event = OtlpEvent(signal, params)  # type: ignore[arg-type]

        channel = params.get("channel")
        self._fan_out(str(channel) if isinstance(channel, str) else "", event)

    def _fan_out(self, channel: str, event: SubscriptionEvent) -> None:
        queue = self._subscriptions.get(channel)
        if queue is not None:
            queue.publish(event)
        self._events.publish(ClientEvent(channel, event))
