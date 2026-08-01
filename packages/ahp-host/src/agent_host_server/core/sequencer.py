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
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from agent_host_server.reducers import REDUCERS

__all__ = ["Sequencer", "Subscriber"]

_log = logging.getLogger(__name__)


class Subscriber(Protocol):
    """A connection, from the sequencer's point of view."""

    client_id: str

    def enqueue(self, message: Mapping[str, Any]) -> None:
        """Queue an outbound message. MUST NOT block and MUST NOT reorder."""


class Sequencer:
    def __init__(self, *, replay_limit: int = 4096) -> None:
        self._lock = asyncio.Lock()
        self._seq = 0
        self._states: dict[str, Any] = {}
        #: channel URI -> reducer name, recorded when the channel is created.
        self._reducers: dict[str, str] = {}
        self._log: deque[dict[str, Any]] = deque(maxlen=replay_limit)
        self._subscribers: dict[str, set[Subscriber]] = {}

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

    def has_channel(self, uri: str) -> bool:
        return uri in self._states

    def state_of(self, uri: str) -> Any:
        return self._states.get(uri)

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

            self._seq += 1
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
            self._log.append(envelope)

            message = {"jsonrpc": "2.0", "method": "action", "params": envelope}
            for subscriber in self._subscribers.get(channel, ()):
                subscriber.enqueue(message)

            return envelope

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
            self._subscribers.setdefault(channel, set()).add(subscriber)
            if channel not in self._states:
                return None
            return {
                "resource": channel,
                "state": self._states[channel],
                "fromSeq": self._seq,
            }

    async def unsubscribe(self, subscriber: Subscriber, channel: str) -> None:
        async with self._lock:
            self._subscribers.get(channel, set()).discard(subscriber)

    async def unsubscribe_all(self, subscriber: Subscriber) -> None:
        async with self._lock:
            for subscribers in self._subscribers.values():
                subscribers.discard(subscriber)

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

            oldest = self._log[0]["serverSeq"] if self._log else self._seq + 1
            can_replay = last_seen_server_seq >= oldest - 1

            if can_replay:
                actions = [
                    envelope
                    for envelope in self._log
                    if envelope["serverSeq"] > last_seen_server_seq and envelope["channel"] in known
                ]
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
