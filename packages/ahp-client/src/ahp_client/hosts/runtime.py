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
from typing import Any, Final, Literal

from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import Transport
from ahp_protocol.types import JsonObject

from ahp_client.client.client import AhpClient, ClientConfig, ServerRequestHandler
from ahp_client.client.errors import AhpClientError, RpcError
from ahp_client.client.events import ClientEvent, Diagnostic, DroppedEvents
from ahp_client.client.mirror import ApplyOutcome, GapPolicy, PendingPolicy, StateMirror
from ahp_client.client.queue import BroadcastQueue, BroadcastReader
from ahp_client.hosts.client_id_store import ClientIdStore, InMemoryClientIdStore
from ahp_client.hosts.policy import ReconnectPolicy, exponential_policy

__all__ = [
    "AuthCheck",
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

#: Re-run against the fresh client after **every** successful handshake --
#: reconnects included -- before the state flips to ``connected``. See
#: :attr:`HostConfig.auth_check`.
AuthCheck = Callable[[AhpClient], Awaitable[None]]


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
    #: cannot see agents, sessions or terminals appear. An entry is either a
    #: bare URI or ``(uri, reducer_name)`` -- the caller stating the channel
    #: kind it already knows, which is invariant 1's preferred source. A bare
    #: non-root URI states no kind; its snapshot falls back to
    #: ``reducer_for_state`` shape-sniffing at apply time, which declines an
    #: ambiguous snapshot rather than guessing.
    initial_subscriptions: tuple[str | tuple[str, str], ...] = (ROOT_URI,)
    client_config: ClientConfig = field(default_factory=ClientConfig)
    reconnect_policy: ReconnectPolicy = field(default_factory=exponential_policy)
    pending_policy: PendingPolicy = PendingPolicy.VSCODE
    gap_policy: GapPolicy = GapPolicy.WARN
    #: Installed on **every** freshly built client, before the handshake. This is
    #: what makes the reverse direction survive a reconnect.
    server_request_handler: ServerRequestHandler | None = None
    client_id_store: ClientIdStore | None = None
    #: Plan section 6.3: "Reconnect must re-check authentication."
    #: ``auth/required`` is ephemeral and never replayed (spec
    #: ``authentication.md``, Auth Expiry), so a reconnect silently loses every
    #: outstanding challenge -- and only the supervisor knows a reconnect
    #: happened. Awaited against the fresh client after every successful
    #: handshake, before the state flips to ``connected``, so a caller released
    #: by ``start(wait=True)`` is never handed a connection nobody re-verified.
    #: A failure here fails the attempt and is classified by the reconnect
    #: policy like any other refusal.
    auth_check: AuthCheck | None = None


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
        #: Where the previous client's request-id and clientSeq counters
        #: stopped. "Ids never reset across transport swaps" (plan section 6.1;
        #: VS Code's first frame on a fresh socket carried id 66) is a promise
        #: only this layer can keep, because one `AhpClient` is one transport.
        self._counter_seed: tuple[int, int] = (1, 1)
        self._generation = 0
        self._server_seq = 0
        #: ``None`` -- never ``""`` -- marks a kind the caller did not state:
        #: ``StateMirror.apply_snapshot`` treats only ``None`` as "infer from
        #: the state's shape", while ``""`` reads as a reducer name and fails
        #: ``bind()`` with a ``KeyError`` on every reconnect that takes a
        #: snapshot path, until the policy exhausts into ``failed``.
        self._subscriptions: dict[str, str | None] = {}
        #: How many callers asked for each channel. See :meth:`subscribe`.
        self._holders: dict[str, int] = {}
        self._mirror = StateMirror(
            client_id="",
            gap_policy=config.gap_policy,
            on_diagnostic=lambda d: self._diagnostics.publish(d),
        )
        # Bounded, and therefore lossy -- which is right for a tap (ADR 0002) and
        # only tolerable if the loss is *reported*. `AhpClient` wires the same
        # queue this way; this one did not, so a reader slower than a flooding
        # pty was fast-forwarded past its own `TerminalRefused` with nothing
        # anywhere saying so.
        self._events: BroadcastQueue[ClientEvent] = BroadcastQueue(
            4096, on_drop=lambda n: self._diagnostics.publish(DroppedEvents("host-events", n))
        )
        self._states: BroadcastQueue[HostState] = BroadcastQueue(256)
        self._diagnostics: BroadcastQueue[Diagnostic] = BroadcastQueue(1024)
        self._shutdown = ShutdownSignal("shutdown")
        self._manual = ShutdownSignal("manual-reconnect")
        self._supervisor: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        #: The other way `_await_connected` can end. `_supervise` reaches a
        #: terminal `failed` and returns without touching `_connected` or
        #: `_shutdown`, so waiting on those two alone makes every permanent
        #: refusal an unobservable hang.
        self._terminal = ShutdownSignal("terminal")
        self._failure: BaseException | None = None
        self.protocol_version: str | None = None
        self.default_directory: str | None = None
        self.completion_trigger_characters: tuple[str, ...] = ()
        #: ``InitializeResult.terminalCommandPrefix`` -- ``"!"`` by convention,
        #: ``None`` when the host supports no prefix. Absence and ``""`` are the
        #: same answer and both mean "do not offer the shorthand".
        self.terminal_command_prefix: str | None = None
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
        self._client_id = await self._resolve_client_id()
        self._mirror._client_id = self._client_id
        self._supervisor = asyncio.get_running_loop().create_task(
            self._supervise(), name=f"ahp-host-{self._config.label}"
        )
        if wait:
            await self._await_connected()

    async def _await_connected(self) -> None:
        """Wait for the connection, or for the news that there will not be one.

        The classification half of a permanent refusal already worked --
        `should_retry` declines a version disagreement and the supervisor
        broadcasts `failed` on `state_changes()`. The release half is this:
        without it `start(wait=True)`, the default and what `connect()` uses,
        is unsatisfiable against `-32005`, a policy close, or an exhausted
        attempt budget, and the caller hangs with no exception to catch.
        """
        async with link(self._shutdown, self._terminal) as waiters:
            try:
                await race(self._connected.wait(), waiters)
            except asyncio.CancelledError:
                # Only the terminal arm is ours to translate; a shutdown -- or a
                # genuine cancellation of the caller -- still unwinds as one.
                if not self._terminal.triggered:
                    raise
        if self._connected.is_set() or not self._terminal.triggered:
            return
        # Raised rather than returned: `start()` promises a connection, and a
        # caller that gets one silently has no reason to consult `state`.
        raise self._failure or HostNotConnected(
            f"{self._config.label} gave up connecting and will not retry"
        )

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

    def subscribed(self, uri: str) -> bool:
        """Whether this runtime still holds a subscription to *uri*.

        The one honest answer to "is this handle still connected to anything".
        A dropped channel keeps answering every read with an empty state, so a
        `Terminal` or `Changeset` built over it reports a settled, empty,
        non-failed resource -- which reads as *nothing happened* rather than as
        *nobody is listening*.
        """
        return uri in self._subscriptions

    async def subscribe(self, uri: str, reducer_name: str) -> None:
        """Subscribe and remember, so a reconnect brings it back.

        Tracked **before** the request goes out: subscribing while disconnected
        records the intent locally and raises, and the next successful connect
        threads it into the handshake.

        A refusal is the other case, and it is rolled back -- symmetry with
        ``AhpClient.subscribe``, which rolls its own queue back for exactly this
        reason. The two halves disagreeing is worse than either: a ``-32009``
        would otherwise leave a bound, snapshot-less channel in the mirror and a
        subscription the runtime re-requests on every reconnect, where the host
        can only decline it again. Only an ``RpcError`` rolls back -- the host
        answered, and the answer was no. A transport failure is not an answer,
        and the reconnect must still bring the subscription back.

        **Held by count, released by count.** Two handles on one channel is the
        ordinary case -- `Session.open_chat`, `Session.open_changeset` and
        `Client.open_terminal` all mint a fresh object per call and none of them
        memoises -- and an unrefcounted `unsubscribe` from either one blinds the
        other with no error anywhere: the survivor's state goes empty, its waits
        return instantly, and its dispatches vanish into a channel this client
        no longer receives.
        """
        existed = uri in self._subscriptions
        self._subscriptions[uri] = reducer_name
        self._holders[uri] = self._holders.get(uri, 0) + 1
        self._mirror.bind(uri, reducer_name)
        client = self.client()
        try:
            result, _subscription = await client.subscribe(uri)
        except RpcError:
            # Only what this call added: tearing down a channel another caller
            # was already subscribed to would blind them over our refusal.
            self._release(uri, drop=not existed)
            raise
        snapshot = result.get("snapshot")
        if isinstance(snapshot, Mapping):
            self._mirror.apply_snapshot(snapshot, reducer_name=reducer_name)

    async def unsubscribe(self, uri: str) -> None:
        """Release one hold. The channel goes when the last one does."""
        if self._release(uri, drop=True):
            return
        if self._client is not None:
            await self._client.unsubscribe(uri)

    def _release(self, uri: str, *, drop: bool) -> bool:
        """Drop one hold on *uri*; report whether others remain."""
        remaining = max(0, self._holders.get(uri, 0) - 1)
        if remaining:
            self._holders[uri] = remaining
            return True
        self._holders.pop(uri, None)
        if drop:
            self._subscriptions.pop(uri, None)
            self._mirror.drop(uri)
        return False

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
            try:
                await self._drain(events)
            finally:
                # Runs even when `_drain` re-raises a genuine external
                # cancellation (see its comment): the transport must not be
                # leaked just because the cancellation is about to propagate
                # out of this task rather than being absorbed as an ordinary
                # disconnect.
                await self._tear_down_client()
                self._connected.clear()
            if self._shutdown.triggered:
                return
            self._manual.reset()

    async def _connect_once(self) -> BroadcastReader[ClientEvent]:
        """One attempt, linked and abortable end to end.

        The link comes FIRST -- plan section 6.3's opening step -- and every
        await in the attempt races it. Without that, ``reconnect_now()``
        against a hung dial or handshake only took effect once the attempt
        resolved on its own, which against a black-holed host is never.
        """
        try:
            async with link(self._shutdown, self._manual) as waiters:
                return await self._attempt(waiters)
        except asyncio.CancelledError:
            # Only the manual arm is ours to translate: an aborted attempt is
            # data for the retry loop, which resets the trigger and dials again
            # immediately. A shutdown -- or a genuine cancellation of the
            # supervisor -- must keep unwinding as cancellation.
            if self._manual.triggered and not self._shutdown.triggered:
                raise HostNotConnected(
                    f"{self._config.label} attempt aborted by reconnect_now()"
                ) from None
            raise

    async def _attempt(
        self, waiters: Sequence[asyncio.Future[Any]]
    ) -> BroadcastReader[ClientEvent]:
        transport = await race(self._config.transport_factory(), waiters)
        first_request_id, first_client_seq = self._counter_seed
        client = AhpClient(
            transport,
            self._config.client_config,
            first_request_id=first_request_id,
            first_client_seq=first_client_seq,
        )
        if self._config.server_request_handler is not None:
            client.set_server_request_handler(self._config.server_request_handler)
        client.set_state_mirror(self._mirror)

        succeeded = False
        try:
            await race(client.connect(), waiters)
            # Attach BEFORE the handshake: anything the host pushes between its
            # response and the drain loop starting would otherwise be lost,
            # because a late reader sees no replay.
            events = client.events()
            prior = tuple(self._subscriptions)
            can_reconnect = self._server_seq > 0 and bool(prior)
            arm = "snapshot"
            acknowledged: list[int] = []

            if can_reconnect:
                try:
                    result = await race(
                        client.reconnect(
                            client_id=self._client_id,
                            last_seen_server_seq=self._server_seq,
                            subscriptions=list(prior),
                        ),
                        waiters,
                    )
                    arm, acknowledged = await self._absorb_reconnect(result, prior=frozenset(prior))
                except RpcError:
                    # An RPC-level refusal means the host cannot resume us --
                    # too much elapsed, or it forgot the id. Fall back. A
                    # transport error is a different thing entirely and must
                    # propagate to the retry loop.
                    await race(self._absorb_initialize(client, prior), waiters)
            else:
                await race(self._absorb_initialize(client, prior), waiters)

            # Plan section 6.3: "Reconnect must re-check authentication."
            # `auth/required` is never replayed, so this hook is the one place
            # an outstanding challenge can be re-checked -- run before
            # `connected` flips, and before `listSessions`, which against an
            # auth-guarded host is the first request that would need it.
            if self._config.auth_check is not None:
                await race(self._config.auth_check(client), waiters)

            resend = self._mirror.on_reconnect(
                policy=self._config.pending_policy, arm=arm, acknowledged=acknowledged
            )
            # The half ADR 0006b describes and nothing implemented. A pending
            # entry the policy KEEPS is one whose `dispatchAction` frame may
            # never have left -- the socket can die inside the write loop -- so
            # keeping it without re-sending renders an optimistic turn the host
            # has never heard of, with no echo that can ever retire it. Sent
            # before `connected`, so a caller that dispatches the moment it sees
            # the state cannot get ahead of the catch-up.
            for uri, entry in resend:
                client.redispatch(uri, entry.action, entry.client_seq)

            # Best-effort: a host that cannot list sessions is still usable.
            with contextlib.suppress(Exception):
                listing = await race(client.list_sessions(), waiters)
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
        kinds = _subscription_kinds(self._config.initial_subscriptions)
        result = await client.initialize(
            client_id=self._client_id,
            initial_subscriptions=list(prior) or list(kinds),
        )
        version = result.get("protocolVersion")
        self.protocol_version = version if isinstance(version, str) else None
        directory = result.get("defaultDirectory")
        self.default_directory = directory if isinstance(directory, str) else None
        triggers = result.get("completionTriggerCharacters")
        self.completion_trigger_characters = (
            tuple(str(t) for t in triggers) if isinstance(triggers, list) else ()
        )
        # Kept for the same reason `completionTriggerCharacters` is: it is an
        # affordance the host advertises and only the client can implement. A
        # client that drops it cannot offer the `!command` shorthand at all, and
        # the two fields are otherwise identical in kind -- one was absorbed and
        # the other fell on the floor.
        prefix = result.get("terminalCommandPrefix")
        self.terminal_command_prefix = prefix if isinstance(prefix, str) and prefix else None
        seq = result.get("serverSeq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            self._server_seq = max(self._server_seq, seq)
        # Recorded BEFORE the snapshots loop, so a kind the caller stated in
        # `initial_subscriptions` is what binds -- and `setdefault`, so a name
        # `subscribe()` already stored is never clobbered by config intent.
        for uri, kind in kinds.items():
            self._subscriptions.setdefault(uri, kind)
        for snapshot in result.get("snapshots") or []:
            if isinstance(snapshot, Mapping):
                uri = str(snapshot.get("resource", ""))
                self._mirror.apply_snapshot(snapshot, reducer_name=self._subscriptions.get(uri))

    async def _absorb_reconnect(
        self, result: Mapping[str, Any], *, prior: frozenset[str]
    ) -> tuple[str, list[int]]:
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
                outcome = self._mirror.apply(envelope)
                # `lastSeenServerSeq` is one scalar over a host-global counter,
                # so it cannot express "and I already have this channel up to
                # 40" -- the host replays from the scalar and is right to. The
                # per-channel `Snapshot.fromSeq` baseline is the designed
                # defence and the mirror applies it; publishing what the mirror
                # discarded would hand a consumer a duplicate of an event it has
                # already seen, which is worse than the gap it is trying to fill.
                if outcome is not ApplyOutcome.STALE:
                    self._events.publish(
                        ClientEvent(str(envelope.get("channel", "")), _action_event(envelope))
                    )
                seq_value = envelope.get("serverSeq")
                if isinstance(seq_value, int) and not isinstance(seq_value, bool):
                    self._server_seq = max(self._server_seq, seq_value)
            return "replay", acknowledged

        snapshots = result.get("snapshots") or []
        surviving = {str(s.get("resource", "")) for s in snapshots if isinstance(s, Mapping)}
        # Keep a URI iff surviving *or not prior* (plan section 6.3): one
        # subscribed while the request was in flight cannot be in the host's
        # answer, and pruning it would silently discard the recorded intent
        # `subscribe()` promises the next handshake will carry. Only what the
        # host was actually asked about -- `prior` -- is the host's to decline.
        for uri in list(self._subscriptions):
            if uri not in surviving and uri in prior:
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
                    # Two things arrive here as `CancelledError`: `race()`'s
                    # own "a signal fired" (a fresh exception; the task is
                    # not being cancelled), and a genuine `Task.cancel()` of
                    # the supervisor -- e.g. `asyncio.run()` tearing down a
                    # client its caller never closed. Only the first is ours
                    # to absorb. Returning on the second made `_supervise`
                    # treat it as a disconnect and reconnect, so the task
                    # could never be cancelled and `asyncio.run()` hung on
                    # exit. `cancelling()` tells the two apart exactly,
                    # including when a signal and a real cancel coincide.
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise
                    return
                if event is None:
                    return
                self._track(event)
                self._events.publish(event)

    def _track(self, event: ClientEvent) -> None:
        from ahp_client.client.events import (
            ActionEvent,
            SessionAdded,
            SessionRemoved,
            SessionSummaryChanged,
        )

        payload = event.event
        if isinstance(payload, ActionEvent):
            if payload.server_seq > self._server_seq:
                self._server_seq = payload.server_seq
        elif isinstance(payload, SessionAdded):
            summary = payload.params.get("summary")
            if isinstance(summary, Mapping):
                self.session_summaries[str(summary.get("resource", ""))] = dict(summary)
        elif isinstance(payload, SessionSummaryChanged):
            self._merge_summary(payload.params)
        elif isinstance(payload, SessionRemoved):
            uri = str(payload.params.get("session", ""))
            self.session_summaries.pop(uri, None)
            self._forget_session(uri)

    #: `SessionSummaryChangedParams.changes`: "Identity fields (`resource`,
    #: `provider`, `createdAt`) never change and MUST be omitted by senders;
    #: receivers SHOULD ignore them if present." Dropped rather than trusted,
    #: because a sender that sends them anyway is exactly the sender whose
    #: values are wrong.
    _IDENTITY_FIELDS: Final = ("resource", "provider", "createdAt")

    def _merge_summary(self, params: Mapping[str, Any]) -> None:
        """Apply ``root/sessionSummaryChanged`` to the cached catalog.

        `session_summaries` is seeded from `listSessions` and is precisely the
        cache this notification exists to keep current -- it lets a client
        "stay in sync with in-flight sessions without having to subscribe to
        every session URI individually". Dropping it left titles, statuses and
        timestamps frozen at connect time for the one consumer of the feature.

        A **merge**, not a replace: `changes` is a genuine partial and the host
        sends bare ones (a lone `{"status": 1}` while a turn runs). And an
        unknown session is ignored, per the same schema: the notification "is
        not a substitute for `root/sessionAdded`", so inventing a catalog entry
        from a partial would publish a summary with no `resource` or `provider`.
        """
        uri = str(params.get("session", ""))
        cached = self.session_summaries.get(uri)
        changes = params.get("changes")
        if cached is None or not isinstance(changes, Mapping):
            return
        cached.update(
            {k: v for k, v in changes.items() if k not in self._IDENTITY_FIELDS},
        )

    def _forget_session(self, uri: str) -> None:
        """Drop a disposed session's channel and every chat channel it owned.

        `reconnect.missing` only exists on the reconnect path; on a live
        connection `root/sessionRemoved` is the whole mechanism, because there
        is no `session/disposed` action for the host to publish. Left tracked,
        the runtime keeps mirroring a session the host has forgotten -- still
        reporting `lifecycle: "ready"` -- and asks to resubscribe to it on every
        subsequent reconnect, where it can only come back as `missing`.

        The chat list is read out of the mirror *before* the session state goes,
        because afterwards nothing anywhere records which chats were its.
        """
        if not uri:
            return
        state = self._mirror.state(uri)
        chats = state.get("chats") if isinstance(state, Mapping) else None
        doomed = [uri]
        if isinstance(chats, list):
            doomed += [str(c.get("resource", "")) for c in chats if isinstance(c, Mapping)]
        for channel in doomed:
            if channel:
                self._subscriptions.pop(channel, None)
                self._mirror.drop(channel)

    async def _tear_down_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            # Captured before shutdown so the successor resumes numbering where
            # this client stopped, keeping ids monotonic across transport swaps.
            self._counter_seed = (client.next_request_id, client.next_client_seq)
            with contextlib.suppress(Exception):
                await client.shutdown()

    def _transition(self, state: HostState) -> None:
        self._state = state
        if state.status == "failed":
            # Released here rather than at each `return` in `_supervise`, so the
            # two ways to reach terminal -- a refusal the policy declines and an
            # exhausted budget -- cannot drift apart.
            self._failure = state.error
            self._terminal.trigger()
        self._states.publish(state)

    async def _resolve_client_id(self) -> str:
        """Explicit -> stored -> fresh ``uuid4()``, per plan section 6.3.

        The resolved value is ALWAYS written back -- an explicitly-supplied id
        included, which is why the explicit case routes through here rather
        than short-circuiting past the store: a process that passes the id
        once and later relies on the store must not come back as a different
        client and silently lose its reconnect identity.
        """
        stored = await self._store.load(self._config.label)
        client_id = self._config.client_id or stored or str(uuid.uuid4())
        await self._store.store(self._config.label, client_id)
        return client_id


def _subscription_kinds(
    entries: Sequence[str | tuple[str, str]],
) -> dict[str, str | None]:
    """``HostConfig.initial_subscriptions`` as ``uri -> reducer name or None``.

    ``None`` -- never ``""`` -- is what makes the bare-URI form survive a second
    connection: only ``None`` engages ``apply_snapshot``'s shape-sniffing
    fallback, which is exactly what the plan says the fallback is *for* -- a
    snapshot whose kind the caller could not state. The root URI is the one
    bare form that does state its kind.
    """
    kinds: dict[str, str | None] = {}
    for entry in entries:
        if isinstance(entry, str):
            kinds[entry] = "root" if entry == ROOT_URI else None
        else:
            uri, kind = entry
            kinds[uri] = kind
    return kinds


def _action_event(envelope: Mapping[str, Any]) -> Any:
    from ahp_client.client.events import ActionEvent

    return ActionEvent(dict(envelope))
