"""The sequencer: authoritative state, ordering, replay and fan-out.

Total ordering is a protocol guarantee, so it is enforced structurally rather
than hoped for. Everything that could break it happens inside **one** critical
section, in this order:

    assign serverSeq -> apply the reducer -> append to the replay log
                     -> enqueue to every subscriber

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
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from agent_host_server.core.channels import reducer_name_for
from agent_host_server.reducers import REDUCERS

__all__ = ["Sequencer", "Subscriber"]


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
        self._log: deque[dict[str, Any]] = deque(maxlen=replay_limit)
        self._subscribers: dict[str, set[Subscriber]] = {}

    @property
    def server_seq(self) -> int:
        """The current global sequence. Read-only outside the critical section."""
        return self._seq

    # ─── state registration ──────────────────────────────────────────────

    async def register_channel(self, uri: str, initial_state: Any) -> None:
        """Create a channel's authoritative state."""
        async with self._lock:
            self._states[uri] = initial_state

    async def drop_channel(self, uri: str) -> None:
        async with self._lock:
            self._states.pop(uri, None)
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
            if rejection_reason is None:
                reducer_name = reducer_name_for(channel)
                if reducer_name is not None:
                    reducer = REDUCERS[reducer_name]
                    self._states[channel] = reducer(self._states[channel], action)

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
