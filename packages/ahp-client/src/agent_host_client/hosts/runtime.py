"""The per-host supervisor: one connection at a time, reconnected forever.

Everything reconnect-shaped lives here and nothing lives in ``AhpClient``. A
dead transport cannot be revived, so each attempt builds a *fresh* client, and
the supervisor is what carries subscriptions, the client id and the sequence
high-water mark across the gap.

The connect sequence is a line-by-line port of the TypeScript supervisor, whose
ordering is not incidental: the event stream is attached before the handshake,
replay is applied before the state flips to connected, and the generation
counter bumps in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import Transport
from agent_host_protocol.types import JsonObject

from agent_host_client.client.client import AhpClient, ClientConfig, ServerRequestHandler
from agent_host_client.client.errors import AhpClientError, RpcError
from agent_host_client.client.events import ClientEvent, Diagnostic
from agent_host_client.client.mirror import GapPolicy, PendingPolicy, StateMirror
from agent_host_client.client.queue import BroadcastQueue, BroadcastReader
from agent_host_client.hosts.client_id_store import ClientIdStore, InMemoryClientIdStore
from agent_host_client.hosts.policy import ReconnectPolicy, exponential_policy

__all__ = [
    "HostConfig",
    "HostNotConnected",
    "HostRuntime",
    "HostShutDown",
    "HostState",
    "HostStatus",
    "ShutdownSignal",
    "TransportFactory",
    "link",
]

HostStatus = Literal["disconnected", "connecting", "connected", "reconnecting", "failed"]

TransportFactory = Callable[[], Awaitable[Transport]]


class ShutdownSignal:
    """An abortable one-shot, standing in for the reference clients' `AbortSignal`.

    Deliberately **not** named ``CancelScope``: ``anyio.CancelScope`` is a
    well-known, semantically different object -- it cancels a task scope, this
    is a flag -- and confusing the two in the files where cancellation
    correctness matters is exactly the mistake to design out.
    """

    __slots__ = ("_event", "name")

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._event = asyncio.Event()

    @property
    def triggered(self) -> bool:
        return self._event.is_set()

    def trigger(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event = asyncio.Event()

    async def wait(self) -> None:
        await self._event.wait()


@contextlib.asynccontextmanager
async def link(*signals: ShutdownSignal) -> Any:
    """Race a body against any of *signals*, detaching cleanly on the way out.

    A **context manager**, so the listener leak the reference implementation
    regression-tests for is structurally impossible rather than dependent on
    somebody remembering a ``finally``. Every reconnect cycle attaches to the
    same long-lived signals, so a leak here grows without bound.
    """
    waiters = [asyncio.ensure_future(signal.wait()) for signal in signals]
    try:
        yield waiters
    finally:
        for waiter in waiters:
            waiter.cancel()
        for waiter in waiters:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await waiter


async def race(awaitable: Awaitable[Any], waiters: Sequence[asyncio.Future[Any]]) -> Any:
    """First result wins; the losers are cancelled **and awaited**.

    A bare ``asyncio.wait(FIRST_COMPLETED)`` leaves the loser pending and
    produces "Task exception was never retrieved" in every log, at a moment
    unrelated to the code that caused it.
    """
    primary = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait([primary, *waiters], return_when=asyncio.FIRST_COMPLETED)
    if primary in done:
        return primary.result()
    primary.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await primary
    raise asyncio.CancelledError("aborted")


class HostShutDown(AhpClientError):
    """The runtime was torn down. Permanent -- it is not coming back."""


class HostNotConnected(AhpClientError):
    """Registered but not connected right now. Recoverable; the supervisor retries."""


@dataclass(frozen=True)
class HostConfig:
    transport_factory: TransportFactory
    label: str = "host"
    client_id: str | None = None
    #: Always includes the root channel: a client that does not mirror root
    #: cannot see agents, sessions or terminals appear.
    initial_subscriptions: tuple[str, ...] = (ROOT_URI,)
    client_config: ClientConfig = field(default_factory=ClientConfig)
    reconnect_policy: ReconnectPolicy = field(default_factory=exponential_policy)
    pending_policy: PendingPolicy = PendingPolicy.VSCODE
    gap_policy: GapPolicy = GapPolicy.WARN
    #: Installed on **every** freshly built client, before the handshake. This is
    #: what makes the reverse direction survive a reconnect.
    server_request_handler: ServerRequestHandler | None = None
    client_id_store: ClientIdStore | None = None


@dataclass(frozen=True, slots=True)
class HostState:
    status: HostStatus
    attempt: int = 0
    error: BaseException | None = None


class HostRuntime:
    """Supervises one host: connect, reconnect, replay, fan out."""

    def __init__(self, config: HostConfig) -> None:
        self._config = config
        self._store: ClientIdStore = config.client_id_store or InMemoryClientIdStore()
        self._client_id = config.client_id or ""
        self._state = HostState("disconnected")
        self._client: AhpClient | None = None
        self._generation = 0
        self._server_seq = 0
        self._subscriptions: dict[str, str] = {}
        self._mirror = StateMirror(
            client_id="",
            gap_policy=config.gap_policy,
            on_diagnostic=lambda d: self._diagnostics.publish(d),
        )
        self._events: BroadcastQueue[ClientEvent] = BroadcastQueue(4096)
        self._states: BroadcastQueue[HostState] = BroadcastQueue(256)
        self._diagnostics: BroadcastQueue[Diagnostic] = BroadcastQueue(1024)
        self._shutdown = ShutdownSignal("shutdown")
        self._manual = ShutdownSignal("manual-reconnect")
        self._supervisor: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self.protocol_version: str | None = None
        self.default_directory: str | None = None
        self.completion_trigger_characters: tuple[str, ...] = ()
        self.session_summaries: dict[str, JsonObject] = {}

    # ── observation ──────────────────────────────────────────────────────────

    @property
    def state(self) -> HostState:
        return self._state

    @property
    def generation(self) -> int:
        """Bumped on every successful connect. A handle minted at an older
        generation is talking to a connection that no longer exists."""
        return self._generation

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def mirror(self) -> StateMirror:
        return self._mirror

    @property
    def server_seq(self) -> int:
        return self._server_seq

    def events(self) -> BroadcastReader[ClientEvent]:
        return self._events.reader()

    def state_changes(self) -> BroadcastReader[HostState]:
        return self._states.reader()

    def diagnostics(self) -> BroadcastReader[Diagnostic]:
        return self._diagnostics.reader()

    def client(self) -> AhpClient:
        """The live client, or raise. Never returns a stale one."""
        if self._shutdown.triggered:
            raise HostShutDown(f"{self._config.label} has been shut down")
        if self._client is None or self._state.status != "connected":
            raise HostNotConnected(f"{self._config.label} is not connected")
        return self._client

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self, *, wait: bool = True) -> None:
        if self._supervisor is not None:
            return
        self._client_id = self._config.client_id or await self._resolve_client_id()
        self._mirror._client_id = self._client_id
        self._supervisor = asyncio.get_running_loop().create_task(
            self._supervise(), name=f"ahp-host-{self._config.label}"
        )
        if wait:
            await self._await_connected()

    async def _await_connected(self) -> None:
        async with link(self._shutdown) as waiters:
            await race(self._connected.wait(), waiters)

    async def shutdown(self) -> None:
        self._shutdown.trigger()
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None
        await self._tear_down_client()
        self._transition(HostState("disconnected"))
        self._events.close()
        self._states.close()
        self._diagnostics.close()

    async def reconnect_now(self) -> None:
        """Force a fresh connection attempt without waiting for a drop."""
        self._manual.trigger()

    # ── subscriptions ────────────────────────────────────────────────────────

    async def subscribe(self, uri: str, reducer_name: str) -> None:
        """Subscribe and remember, so a reconnect brings it back.

        Tracked **before** the request goes out: subscribing while disconnected
        records the intent locally and raises, and the next successful connect
        threads it into the handshake.
        """
        self._subscriptions[uri] = reducer_name
        self._mirror.bind(uri, reducer_name)
        client = self.client()
        result, _subscription = await client.subscribe(uri)
        snapshot = result.get("snapshot")
        if isinstance(snapshot, Mapping):
            self._mirror.apply_snapshot(snapshot, reducer_name=reducer_name)

    async def unsubscribe(self, uri: str) -> None:
        self._subscriptions.pop(uri, None)
        self._mirror.drop(uri)
        if self._client is not None:
            await self._client.unsubscribe(uri)

    # ── the loop ─────────────────────────────────────────────────────────────

    async def _supervise(self) -> None:
        attempt = 0
        while not self._shutdown.triggered:
            attempt += 1
            self._transition(HostState("connecting" if attempt == 1 else "reconnecting", attempt))
            try:
                events = await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failed attempt is data, not a crash
                policy = self._config.reconnect_policy
                if not policy.should_retry(exc):
                    # A permanent refusal -- an expired credential, a policy
                    # close, a version disagreement. Retrying is not merely
                    # futile: it is load against the peer already saying no,
                    # while the surface above shows "reconnecting" forever and
                    # nothing anywhere says "re-authenticate". `failed` is
                    # broadcast on `state_changes()`, which is what an embedder
                    # maps onto a re-login prompt.
                    self._transition(HostState("failed", attempt, exc))
                    return
                self._transition(HostState("reconnecting", attempt, exc))
                if policy.exhausted(attempt):
                    self._transition(HostState("failed", attempt, exc))
                    return
                delay = policy.delay_with_jitter(attempt)
                async with link(self._shutdown, self._manual) as waiters:
                    with contextlib.suppress(asyncio.CancelledError):
                        await race(asyncio.sleep(delay), waiters)
                self._manual.reset()
                continue

            if self._config.reconnect_policy.reset_on_success:
                attempt = 0
            await self._drain(events)
            await self._tear_down_client()
            self._connected.clear()
            if self._shutdown.triggered:
                return
            self._manual.reset()

    async def _connect_once(self) -> BroadcastReader[ClientEvent]:
        transport = await self._config.transport_factory()
        client = AhpClient(transport, self._config.client_config)
        if self._config.server_request_handler is not None:
            client.set_server_request_handler(self._config.server_request_handler)
        client.set_state_mirror(self._mirror)
        await client.connect()
        # Attach BEFORE the handshake: anything the host pushes between its
        # response and the drain loop starting would otherwise be lost, because
        # a late reader sees no replay.
        events = client.events()

        succeeded = False
        try:
            prior = tuple(self._subscriptions)
            can_reconnect = self._server_seq > 0 and bool(prior)
            arm = "snapshot"
            acknowledged: list[int] = []

            if can_reconnect:
                try:
                    result = await client.reconnect(
                        client_id=self._client_id,
                        last_seen_server_seq=self._server_seq,
                        subscriptions=list(prior),
                    )
                    arm, acknowledged = await self._absorb_reconnect(result)
                except RpcError:
                    # An RPC-level refusal means the host cannot resume us --
                    # too much elapsed, or it forgot the id. Fall back. A
                    # transport error is a different thing entirely and must
                    # propagate to the retry loop.
                    await self._absorb_initialize(client, prior)
            else:
                await self._absorb_initialize(client, prior)

            self._mirror.on_reconnect(
                policy=self._config.pending_policy, arm=arm, acknowledged=acknowledged
            )

            # Best-effort: a host that cannot list sessions is still usable.
            with contextlib.suppress(Exception):
                listing = await client.list_sessions()
                items = listing.get("items")
                if isinstance(items, list):
                    self.session_summaries = {
                        str(item.get("resource", "")): dict(item)
                        for item in items
                        if isinstance(item, Mapping)
                    }

            self._generation += 1
            self._client = client
            self._transition(HostState("connected"))
            self._connected.set()
            succeeded = True
            return events
        finally:
            if not succeeded:
                # Do not leak the transport on a half-built connection.
                with contextlib.suppress(Exception):
                    await client.shutdown()

    async def _absorb_initialize(self, client: AhpClient, prior: Sequence[str]) -> None:
        result = await client.initialize(
            client_id=self._client_id,
            initial_subscriptions=list(prior) or list(self._config.initial_subscriptions),
        )
        version = result.get("protocolVersion")
        self.protocol_version = version if isinstance(version, str) else None
        directory = result.get("defaultDirectory")
        self.default_directory = directory if isinstance(directory, str) else None
        triggers = result.get("completionTriggerCharacters")
        self.completion_trigger_characters = (
            tuple(str(t) for t in triggers) if isinstance(triggers, list) else ()
        )
        seq = result.get("serverSeq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            self._server_seq = max(self._server_seq, seq)
        for snapshot in result.get("snapshots") or []:
            if isinstance(snapshot, Mapping):
                uri = str(snapshot.get("resource", ""))
                self._mirror.apply_snapshot(snapshot, reducer_name=self._subscriptions.get(uri))
        for uri in self._config.initial_subscriptions:
            self._subscriptions.setdefault(uri, "root" if uri == ROOT_URI else "")

    async def _absorb_reconnect(self, result: Mapping[str, Any]) -> tuple[str, list[int]]:
        kind = result.get("type")
        acknowledged: list[int] = []
        if kind == "replay":
            missing = result.get("missing") or []
            for uri in missing:
                self._subscriptions.pop(str(uri), None)
            self._mirror.mark_missing(str(uri) for uri in missing)
            for envelope in result.get("actions") or []:
                if not isinstance(envelope, Mapping):
                    continue
                origin = envelope.get("origin")
                if isinstance(origin, Mapping) and origin.get("clientId") == self._client_id:
                    seq = origin.get("clientSeq")
                    if isinstance(seq, int) and not isinstance(seq, bool):
                        acknowledged.append(seq)
                # Applied BEFORE the state flips to connected, so a consumer
                # observing `connected` already sees the catch-up.
                self._mirror.apply(envelope)
                self._events.publish(
                    ClientEvent(str(envelope.get("channel", "")), _action_event(envelope))
                )
                seq_value = envelope.get("serverSeq")
                if isinstance(seq_value, int) and not isinstance(seq_value, bool):
                    self._server_seq = max(self._server_seq, seq_value)
            return "replay", acknowledged

        snapshots = result.get("snapshots") or []
        surviving = {str(s.get("resource", "")) for s in snapshots if isinstance(s, Mapping)}
        # Keep a URI if it survived, or if it was added while the request was in
        # flight -- dropping the latter would silently lose a subscription the
        # caller just made.
        for uri in list(self._subscriptions):
            if uri not in surviving:
                self._subscriptions.pop(uri, None)
                self._mirror.drop(uri)
        for snapshot in snapshots:
            if not isinstance(snapshot, Mapping):
                continue
            uri = str(snapshot.get("resource", ""))
            self._mirror.apply_snapshot(snapshot, reducer_name=self._subscriptions.get(uri))
            from_seq = snapshot.get("fromSeq")
            if isinstance(from_seq, int) and not isinstance(from_seq, bool):
                self._server_seq = max(self._server_seq, from_seq)
        return "snapshot", acknowledged

    async def _drain(self, events: BroadcastReader[ClientEvent]) -> None:
        async def next_event() -> ClientEvent | None:
            """End-of-stream as a value, not an exception.

            A `StopAsyncIteration` raised inside a racing task finishes a task
            nobody retrieves, which asyncio then reports at an unrelated moment
            with a stack pointing nowhere useful.
            """
            try:
                return await events.__anext__()
            except StopAsyncIteration:
                return None

        async with link(self._shutdown, self._manual) as waiters:
            while True:
                try:
                    event = await race(next_event(), waiters)
                except asyncio.CancelledError:
                    return
                if event is None:
                    return
                self._track(event)
                self._events.publish(event)

    def _track(self, event: ClientEvent) -> None:
        from agent_host_client.client.events import (
            ActionEvent,
            SessionAdded,
            SessionRemoved,
        )

        payload = event.event
        if isinstance(payload, ActionEvent):
            if payload.server_seq > self._server_seq:
                self._server_seq = payload.server_seq
        elif isinstance(payload, SessionAdded):
            summary = payload.params.get("summary")
            if isinstance(summary, Mapping):
                self.session_summaries[str(summary.get("resource", ""))] = dict(summary)
        elif isinstance(payload, SessionRemoved):
            self.session_summaries.pop(str(payload.params.get("session", "")), None)

    async def _tear_down_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.shutdown()

    def _transition(self, state: HostState) -> None:
        self._state = state
        self._states.publish(state)

    async def _resolve_client_id(self) -> str:
        stored = await self._store.load(self._config.label)
        client_id = stored or str(uuid.uuid4())
        # Always written back, so an explicitly-supplied id is persisted too and
        # the next launch reuses it without the caller having to.
        await self._store.store(self._config.label, client_id)
        return client_id


def _action_event(envelope: Mapping[str, Any]) -> Any:
    from agent_host_client.client.events import ActionEvent

    return ActionEvent(dict(envelope))
