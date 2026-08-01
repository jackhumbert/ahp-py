"""The sequencer: authoritative state, ordering, replay and fan-out.

Total ordering is a protocol guarantee, so it is enforced structurally rather
than hoped for. Everything that could break it happens inside **one** critical
section, in this order:

    apply the reducer -> assign serverSeq -> append to the replay log
                      -> enqueue to every subscriber

The reducer runs *before* the sequence number is taken, so a reducer that
raises cannot consume one: a burnt `serverSeq` would be missing from the log
forever, and replay above a `Snapshot.fromSeq` could never fill the hole.

``serverSeq`` is a single **host-global** monotonic counter -- not per-channel
and not per-connection. ``reconnect`` carries exactly one scalar
``lastSeenServerSeq`` covering every subscription, which a per-channel counter
could not answer.

Fan-out is an enqueue, never a send: each connection owns a queue drained by a
single writer task. The one existing third-party host loops over connections
calling an unawaited ``send``, which can deliver envelopes out of order and
break every client mirror -- ordering *is* the correctness model here.

**The replay log is budgeted per channel, not globally.** One shared buffer
looks equivalent and is not: a terminal streaming output at fifty frames a
second exhausts a 4096-entry global buffer in about eighty seconds, after which
*every* channel's ``reconnect`` degrades to a full snapshot -- including a chat
that produced no traffic at all. One noisy channel would deny replay to all the
quiet ones. So each channel keeps its own bounded log and its own eviction
watermark, and a client's ability to replay is decided per channel.

Not implemented, deliberately: ``SubscriptionDeliveryOptions.maxLatencyMs``.
It is the protocol's own coalescing knob, but no in-tree client sends it -- VS
Code's ``subscribe()`` transmits ``{channel}`` and nothing else -- and honouring
it means buffering on the path that carries the ordering guarantee. It is listed
in ``docs/roadmap.md`` §5 rather than guessed at here.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from agent_host_server.core.seq import InMemorySequence, SequenceAllocator
from agent_host_server.reducers import REDUCERS

__all__ = ["Sequencer", "Subscriber", "SubscriptionObserver"]

_log = logging.getLogger(__name__)


class Subscriber(Protocol):
    """A connection, from the sequencer's point of view."""

    client_id: str

    def enqueue(self, message: Mapping[str, Any]) -> None:
        """Queue an outbound message. MUST NOT block and MUST NOT reorder."""


class SubscriptionObserver(Protocol):
    """Told when a channel gains its first subscriber, or loses its last.

    Some channels are only worth *doing work for* while somebody is watching --
    a resource watch should not hold a filesystem watcher open for nobody. The
    edges are what matter, so these fire on the transitions, not on every
    subscribe.
    """

    def channel_observed(self, channel: str) -> None:
        """The channel went from zero subscribers to one."""

    def channel_unobserved(self, channel: str) -> None:
        """The channel went from one subscriber to zero."""


class Sequencer:
    def __init__(
        self,
        *,
        replay_limit: int = 1024,
        allocator: SequenceAllocator | None = None,
        observer: SubscriptionObserver | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._allocator = allocator or InMemorySequence()
        self._seq = 0
        self._states: dict[str, Any] = {}
        #: channel URI -> reducer name, recorded when the channel is created.
        self._reducers: dict[str, str] = {}
        #: Per channel, so a chatty one cannot evict a quiet one's history.
        self._replay_limit = replay_limit
        self._log: dict[str, deque[dict[str, Any]]] = {}
        #: Per channel, the highest `serverSeq` its log has dropped. A client
        #: at or above this can still be replayed; below it, only a snapshot
        #: can tell the truth.
        self._evicted_through: dict[str, int] = {}
        self._subscribers: dict[str, set[Subscriber]] = {}
        self._observer = observer

    @property
    def server_seq(self) -> int:
        """The current global sequence. Read-only outside the critical section."""
        return self._seq

    # ─── state registration ──────────────────────────────────────────────

    async def register_channel(self, uri: str, initial_state: Any, reducer: str) -> None:
        """Create a channel's authoritative state and bind its reducer.

        The reducer is recorded here rather than derived from the URI scheme.
        Session and chat URIs are **client-chosen and opaque**: VS Code uses
        ``<provider>:/<uuid>`` for sessions and
        ``ahp-chat://<chatId>/<base64 session uri>`` for chats, neither of which
        matches the ``ahp-session:`` / ``ahp-chat:`` forms the spec's examples
        use. Routing on the scheme silently applies no reducer at all, so the
        host's state freezes at the snapshot while it keeps broadcasting actions
        -- every client then diverges immediately, with nothing to notice it.
        """
        if reducer not in REDUCERS:
            raise ValueError(f"unknown reducer {reducer!r}")
        async with self._lock:
            self._states[uri] = initial_state
            self._reducers[uri] = reducer

    async def drop_channel(self, uri: str) -> None:
        async with self._lock:
            self._states.pop(uri, None)
            self._reducers.pop(uri, None)
            self._subscribers.pop(uri, None)
            self._log.pop(uri, None)
            self._evicted_through.pop(uri, None)

    def has_channel(self, uri: str) -> bool:
        return uri in self._states

    def state_of(self, uri: str) -> Any:
        return self._states.get(uri)

    def reducer_of(self, uri: str) -> str | None:
        """Which reducer a channel is bound to, or ``None`` if unregistered.

        The only correct way to ask "is this a chat channel?". Classifying by URI
        scheme happens to work for channels this host mints, and breaks the
        moment a client names one -- which is the normal case for sessions,
        chats and terminals alike.
        """
        return self._reducers.get(uri)

    # ─── the critical section ────────────────────────────────────────────

    async def publish(
        self,
        channel: str,
        action: Mapping[str, Any],
        *,
        origin: Mapping[str, Any] | None = None,
        rejection_reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Sequence, apply, log and fan out one action.

        Returns the envelope that was broadcast, or ``None`` when the channel
        does not exist -- the spec requires a host to **silently ignore** an
        action on an unknown channel, with no echo.
        """
        async with self._lock:
            if channel not in self._states:
                return None

            # Reduce FIRST, against a candidate state, and only then take a
            # sequence number. A reducer that raises must not consume a
            # serverSeq: the number would be gone from the log forever, and
            # `Snapshot.fromSeq` replay -- which assumes the log is contiguous
            # above the snapshot -- could never fill the hole. Every client
            # that reconnected across it would silently miss the gap.
            #
            # A fault is turned into a rejection rather than propagated. The
            # inbound path treats a malformed action from an untrusted peer as
            # a protocol error, not as grounds to drop the connection.
            next_state = self._states[channel]
            if rejection_reason is None:
                reducer_name = self._reducers.get(channel)
                if reducer_name is not None:
                    try:
                        next_state = REDUCERS[reducer_name](next_state, action)
                    except Exception as exc:
                        _log.exception("reducer failed on %s", channel)
                        rejection_reason = f"reducer error: {type(exc).__name__}: {exc}"
                        next_state = self._states[channel]

            self._seq = self._allocator.next()
            envelope: dict[str, Any] = {
                "channel": channel,
                "action": dict(action),
                "serverSeq": self._seq,
            }
            if origin is not None:
                envelope["origin"] = dict(origin)
            if rejection_reason is not None:
                envelope["rejectionReason"] = rejection_reason

            # A rejected action is echoed so the client can revert its
            # optimistic prediction, but it is NOT applied.
            self._states[channel] = next_state
            self._append(channel, envelope)

            message = {"jsonrpc": "2.0", "method": "action", "params": envelope}
            for subscriber in self._subscribers.get(channel, ()):
                subscriber.enqueue(message)

            return envelope

    def _append(self, channel: str, envelope: dict[str, Any]) -> None:
        """Add to the channel's replay log, recording anything it pushes out.

        A `deque` with a `maxlen` discards silently, and a silently discarded
        envelope is indistinguishable from one that never happened -- which
        would let `replay` claim a client is up to date across a hole. So the
        eviction is watched for and its sequence number kept.
        """
        entries = self._log.get(channel)
        if entries is None:
            entries = self._log[channel] = deque(maxlen=self._replay_limit)
        if len(entries) == self._replay_limit:
            self._evicted_through[channel] = entries[0]["serverSeq"]
        entries.append(envelope)

    async def notify(self, channel: str, method: str, params: Mapping[str, Any]) -> None:
        """Send a protocol notification to a channel's subscribers.

        Unlike actions these are ephemeral: they carry no ``serverSeq`` and are
        never replayed on reconnect (the client re-fetches instead).
        """
        async with self._lock:
            message = {"jsonrpc": "2.0", "method": method, "params": dict(params)}
            for subscriber in self._subscribers.get(channel, ()):
                subscriber.enqueue(message)

    # ─── subscriptions ───────────────────────────────────────────────────

    async def subscribe(self, subscriber: Subscriber, channel: str) -> dict[str, Any] | None:
        """Register a subscriber and capture its snapshot atomically.

        ``Snapshot.fromSeq`` carries the protocol's only formal ordering rule --
        subsequent actions have ``serverSeq > fromSeq`` -- and no in-tree client
        buffers pre-snapshot envelopes. So registration and capture must happen
        under the same lock, and the caller must write the subscribe response to
        the connection's queue before any action for this channel. Because
        fan-out is an enqueue on that same queue, ordering then follows.

        Returns ``None`` for a stateless or unknown channel.
        """
        async with self._lock:
            subscribers = self._subscribers.setdefault(channel, set())
            first = not subscribers
            subscribers.add(subscriber)
            if first:
                self._notify_observer(channel, observed=True)
            if channel not in self._states:
                return None
            return {
                "resource": channel,
                "state": self._states[channel],
                "fromSeq": self._seq,
            }

    async def unsubscribe(self, subscriber: Subscriber, channel: str) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(channel)
            if subscribers is None or subscriber not in subscribers:
                return
            subscribers.discard(subscriber)
            if not subscribers:
                self._notify_observer(channel, observed=False)

    async def unsubscribe_all(self, subscriber: Subscriber) -> None:
        async with self._lock:
            for channel, subscribers in self._subscribers.items():
                if subscriber not in subscribers:
                    continue
                subscribers.discard(subscriber)
                if not subscribers:
                    self._notify_observer(channel, observed=False)

    def _notify_observer(self, channel: str, *, observed: bool) -> None:
        """Fire a lifecycle edge. Never lets the observer break the sequencer.

        This runs inside the critical section, so an exception escaping here
        would leave the lock held and the host wedged -- and the observer is
        embedder code.
        """
        if self._observer is None:
            return
        try:
            if observed:
                self._observer.channel_observed(channel)
            else:
                self._observer.channel_unobserved(channel)
        except Exception:
            _log.exception("subscription observer failed on %s", channel)

    def subscriptions_of(self, subscriber: Subscriber) -> set[str]:
        return {uri for uri, subs in self._subscribers.items() if subscriber in subs}

    # ─── reconnect ───────────────────────────────────────────────────────

    async def replay(
        self,
        last_seen_server_seq: int,
        subscriptions: Iterable[str],
    ) -> dict[str, Any]:
        """Answer ``reconnect``: replay the gap, or fall back to snapshots.

        The result discriminates on ``type``. ``missing`` lists subscriptions
        that cannot be resumed -- disposed sessions, or resources the client may
        no longer observe -- which clients drop from their local set.
        """
        async with self._lock:
            requested = list(subscriptions)
            known = [uri for uri in requested if uri in self._states]
            missing = [uri for uri in requested if uri not in self._states]

            # A sequence number ahead of ours cannot have come from this
            # process. With an in-memory allocator `serverSeq` restarts at 0
            # when the host does, so a client remembering 38 against a counter
            # at 0 is from a previous epoch. Replaying "nothing since 38" would
            # tell it it is up to date while its state is stale and
            # unrecoverable, which is worse than any gap. Force snapshots.
            #
            # A durable allocator (`core/seq.py`) makes this case rare rather
            # than routine, which is the point of having one.
            from_previous_epoch = last_seen_server_seq > self._seq

            # Decided per channel: one chatty channel evicting its own history
            # must not deny replay to a quiet one the same client also watches.
            # But the answer is still all-or-nothing, because `reconnect` gets
            # one `type` for the whole request -- so a single unreplayable
            # channel means snapshots, and the budget's job is to make that
            # rare rather than to make it partial.
            can_replay = not from_previous_epoch and all(
                last_seen_server_seq >= self._evicted_through.get(uri, 0) for uri in known
            )

            if can_replay:
                actions = sorted(
                    (
                        envelope
                        for uri in known
                        for envelope in self._log.get(uri, ())
                        if envelope["serverSeq"] > last_seen_server_seq
                    ),
                    key=lambda envelope: envelope["serverSeq"],
                )
                return {"type": "replay", "actions": actions, "missing": missing}

            # The gap exceeds the replay buffer: fresh snapshots instead. This
            # is explicitly allowed, and is also the honest answer after a host
            # restart, when the counter no longer relates to what the client saw.
            return {
                "type": "snapshot",
                "snapshots": [
                    {"resource": uri, "state": self._states[uri], "fromSeq": self._seq}
                    for uri in known
                ],
            }
