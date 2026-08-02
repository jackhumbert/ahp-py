"""Reducer-driven state, with the reconciliation the spec actually specifies.

The reference ``AhpStateMirror`` is not the model. It wires four of seven
reducers, silently ignores every ``ahp-chat:`` snapshot, never reads
``envelope.serverSeq``, picks reducers by URI **scheme** (so VS Code's
``<provider>:/<uuid>`` sessions bind nothing), and implements no reconciliation
at all. The model is VS Code's internal ``agentSubscription.ts``, which is the
only working implementation of the algorithm `docs/guide/reconciliation.md`
describes. See ADR 0004.

Three states, per the spec's own vocabulary:

* ``confirmed`` -- everything the host has acknowledged.
* ``pending`` -- our own dispatches, applied optimistically, not yet echoed.
* ``optimistic`` -- ``confirmed`` with ``pending`` replayed on top. Computed,
  never stored, so a foreign action rebases the pending queue for free.

Render ``optimistic``. Trust ``confirmed``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from agent_host_protocol.channels import reducer_for_state
from agent_host_protocol.reducers import REDUCERS, Reducer
from agent_host_protocol.types import JsonObject

from agent_host_client.client.events import ActionRejected, Diagnostic, SequenceGap

__all__ = [
    "ApplyOutcome",
    "ChannelMirror",
    "GapPolicy",
    "PendingAction",
    "PendingPolicy",
    "StateMirror",
]


class ApplyOutcome(Enum):
    """What happened to one envelope. Returned so a caller can react and a test
    can assert on the path taken rather than only on the resulting state."""

    APPLIED = "applied"
    #: Our own action, echoed back refused. The optimistic effect is reverted.
    REJECTED = "rejected"
    #: For a channel with no snapshot yet. Held, not dropped.
    BUFFERED = "buffered"
    #: At or below the channel's snapshot baseline; already accounted for.
    STALE = "stale"
    #: Not a channel we track.
    UNKNOWN_CHANNEL = "unknownChannel"


class GapPolicy(Enum):
    """What to do about a hole in ``serverSeq``.

    Never fatal in any mode. A client sees only the channels it subscribed to,
    so its view of a host-global counter legitimately has holes -- raising would
    make the common case an error. See ADR 0005.
    """

    #: Reference-compatible: apply and say nothing.
    IGNORE = "ignore"
    #: Report on ``diagnostics()``, then apply. The default.
    WARN = "warn"
    #: Additionally mark the channel stale so a caller can re-subscribe.
    RESEED = "reseed"


class PendingPolicy(Enum):
    """What happens to un-echoed dispatches across a reconnect.

    Three references disagree, which is why this is a named switch rather than a
    silent choice (ADR 0006b). ``docs/guide/reconciliation.md:81`` says clear in
    both arms; VS Code re-sends survivors on the replay arm; Swift re-sends
    always.
    """

    #: Default. Replay arm keeps un-acknowledged entries; snapshot arm clears.
    VSCODE = "vscode"
    #: Clear in both arms, as the prose says.
    SPEC = "spec"
    #: Keep in both arms.
    RESEND_ALL = "resendAll"


@dataclass(slots=True)
class PendingAction:
    client_seq: int
    action: JsonObject


@dataclass(slots=True)
class ChannelMirror:
    """One channel's confirmed state, pending queue and sequence baseline."""

    uri: str
    reducer: Reducer
    reducer_name: str
    confirmed: Any = None
    #: ``Snapshot.fromSeq``. Actions at or below this are already in ``confirmed``
    #: -- the protocol's only formal ordering rule, and the only defence against
    #: a late snapshot re-applying what a live action already did.
    from_seq: int = 0
    #: Highest ``serverSeq`` this channel has seen, for gap detection.
    last_seq: int = 0
    has_snapshot: bool = False
    pending: list[PendingAction] = field(default_factory=list)
    #: Envelopes that arrived before the snapshot did.
    buffered: list[JsonObject] = field(default_factory=list)

    @property
    def optimistic(self) -> Any:
        """``confirmed`` with ``pending`` replayed. Computed every time.

        Recomputing is what makes a foreign action rebase the queue for free:
        there is no stored optimistic state to invalidate.
        """
        state = self.confirmed
        for entry in self.pending:
            state = self.reducer(state, entry.action)
        return state


class StateMirror:
    """Every channel this client tracks.

    **Not fed from a subscription queue.** The client applies envelopes here on
    the read path, before fan-out, so state is correct even when every event tap
    has been abandoned. That is what makes bounding the taps safe (ADR 0002).
    """

    def __init__(
        self,
        *,
        client_id: str,
        gap_policy: GapPolicy = GapPolicy.WARN,
        on_diagnostic: Callable[[Diagnostic], None] | None = None,
    ) -> None:
        self._client_id = client_id
        self._gap_policy = gap_policy
        self._on_diagnostic = on_diagnostic
        self._channels: dict[str, ChannelMirror] = {}
        self._stale: set[str] = set()
        self._last_server_seq = 0
        self._thread: int | None = None

    # ── registration ─────────────────────────────────────────────────────────

    def bind(self, uri: str, reducer_name: str) -> ChannelMirror:
        """Bind a reducer to a channel, by **name**, from the caller.

        The caller knows what it subscribed to. Nothing here infers a reducer
        from a URI scheme -- real clients mint URIs the scheme table does not
        describe, and a scheme-routed lookup silently binds nothing, which
        freezes state while actions keep arriving.
        """
        reducer = REDUCERS.get(reducer_name)
        if reducer is None:
            raise KeyError(f"no reducer named {reducer_name!r}; have {sorted(REDUCERS)}")
        existing = self._channels.get(uri)
        if existing is not None and existing.reducer_name == reducer_name:
            return existing
        channel = ChannelMirror(uri=uri, reducer=reducer, reducer_name=reducer_name)
        self._channels[uri] = channel
        return channel

    def drop(self, uri: str) -> None:
        self._channels.pop(uri, None)
        self._stale.discard(uri)

    def mark_missing(self, uris: Iterable[str]) -> None:
        """Forget channels a reconnect reported it cannot resume."""
        for uri in uris:
            self.drop(uri)

    def reset(self) -> None:
        self._channels.clear()
        self._stale.clear()

    # ── reading ──────────────────────────────────────────────────────────────

    @property
    def channels(self) -> Mapping[str, ChannelMirror]:
        return self._channels

    @property
    def last_server_seq(self) -> int:
        return self._last_server_seq

    @property
    def stale(self) -> frozenset[str]:
        """Channels a gap made untrustworthy under :attr:`GapPolicy.RESEED`."""
        return frozenset(self._stale)

    def state(self, uri: str) -> Any:
        """Optimistic state -- what a UI renders."""
        channel = self._channels.get(uri)
        return None if channel is None else channel.optimistic

    def confirmed(self, uri: str) -> Any:
        """Server-acknowledged state -- what a decision is made on.

        Timestamps in particular should be read from here. The chat reducer
        stamps ``modifiedAt`` from the clock at turn end, so replaying a pending
        action produces a *different* stamp than the host's echo will; rendering
        the optimistic one guarantees a visible diff at the end of every turn.
        """
        channel = self._channels.get(uri)
        return None if channel is None else channel.confirmed

    def pending(self, uri: str) -> Sequence[PendingAction]:
        channel = self._channels.get(uri)
        return () if channel is None else tuple(channel.pending)

    # ── writing ──────────────────────────────────────────────────────────────

    def apply_snapshot(
        self, snapshot: Mapping[str, Any], *, reducer_name: str | None = None
    ) -> str:
        """Install a snapshot and replay anything that arrived ahead of it.

        Returns the channel URI. Matching is on ``Snapshot.resource``, never
        positionally: ``initialSubscriptions`` yields snapshots only for
        state-bearing channels, so a stateless channel in that list produces no
        entry and the arrays do not line up.
        """
        self._assert_single_threaded()
        uri = str(snapshot.get("resource", ""))
        state = snapshot.get("state")
        raw_from = snapshot.get("fromSeq")
        from_seq = raw_from if isinstance(raw_from, int) and not isinstance(raw_from, bool) else 0

        name = reducer_name
        if name is None:
            existing = self._channels.get(uri)
            # Shape-sniffing is the fallback for a snapshot we did not ask for.
            name = existing.reducer_name if existing else reducer_for_state(state)
        if name is None:
            return uri

        channel = self.bind(uri, name)
        channel.confirmed = state
        channel.from_seq = from_seq
        channel.last_seq = max(channel.last_seq, from_seq)
        channel.has_snapshot = True
        self._stale.discard(uri)

        # Anything buffered at or below `fromSeq` is already inside this
        # snapshot; the rest is genuine catch-up.
        buffered, channel.buffered = channel.buffered, []
        for envelope in buffered:
            if _server_seq(envelope) > from_seq:
                self.apply(envelope)
        return uri

    def _assert_single_threaded(self) -> None:
        """The reducers read a module-global clock.

        That is safe because no mutation here spans an ``await`` -- a weaker and
        more precise rule than "one task", since ``record_pending`` runs from
        whichever task called ``dispatch``. It stops being true the moment
        reduction happens off the event loop, and
        ``asyncio.to_thread(mirror.apply, envelope)`` breaks it *silently*,
        producing wrong ``modifiedAt`` stamps under a concurrent
        ``frozen_clock``. One integer compare on the hot path buys a loud
        failure instead.
        """
        current = threading.get_ident()
        if self._thread is None:
            self._thread = current
        elif self._thread != current:
            raise RuntimeError(
                "StateMirror was used from two threads; the reducers read a "
                "module-global clock and must never run off the event loop"
            )

    def record_pending(self, uri: str, action: Mapping[str, Any], client_seq: int) -> None:
        """Apply one of our own dispatches optimistically."""
        self._assert_single_threaded()
        channel = self._channels.get(uri)
        if channel is None:
            return
        channel.pending.append(PendingAction(client_seq, dict(action)))

    def apply(self, envelope: Mapping[str, Any]) -> ApplyOutcome:
        """Fold one ``ActionEnvelope`` in. Never raises on protocol data."""
        self._assert_single_threaded()
        uri = str(envelope.get("channel", ""))
        channel = self._channels.get(uri)
        server_seq = _server_seq(envelope)
        if server_seq > self._last_server_seq:
            self._last_server_seq = server_seq

        if channel is None:
            return ApplyOutcome.UNKNOWN_CHANNEL

        if not channel.has_snapshot:
            # Buffered, not dropped. `Snapshot.fromSeq` lets us decide later
            # which of these the snapshot already contains; the reference client
            # discards them and loses whatever arrived during the round trip.
            channel.buffered.append(dict(envelope))
            return ApplyOutcome.BUFFERED

        if server_seq and server_seq <= channel.from_seq:
            return ApplyOutcome.STALE

        self._check_gap(channel, server_seq)

        action = envelope.get("action")
        action = dict(action) if isinstance(action, Mapping) else {}
        origin = envelope.get("origin")
        origin = origin if isinstance(origin, Mapping) else None
        rejection = envelope.get("rejectionReason")
        # Absent and explicitly-null origin are the same thing: server-originated.
        own = origin is not None and origin.get("clientId") == self._client_id
        client_seq = origin.get("clientSeq") if origin is not None else None

        if own and isinstance(rejection, str):
            self._retire(channel, client_seq)
            self._diagnose(ActionRejected(uri, client_seq or 0, rejection, action))
            return ApplyOutcome.REJECTED

        if own:
            # Retire the matching entry, then apply. VS Code matches on an exact
            # clientSeq -- and still applies when nothing matches, which is the
            # arm every reimplementation of this leaves out.
            self._retire(channel, client_seq)

        channel.confirmed = channel.reducer(channel.confirmed, action)
        if server_seq:
            channel.last_seq = server_seq
        return ApplyOutcome.APPLIED

    def on_reconnect(
        self,
        *,
        policy: PendingPolicy,
        arm: str,
        acknowledged: Iterable[int] = (),
    ) -> None:
        """Settle the pending queues after a reconnect.

        *arm* is ``"replay"`` or ``"snapshot"``. The three reference
        implementations disagree about this; see :class:`PendingPolicy`.
        """
        acked = set(acknowledged)
        for channel in self._channels.values():
            if policy is PendingPolicy.RESEND_ALL:
                continue
            if policy is PendingPolicy.SPEC or arm == "snapshot":
                # Predicated on pre-disconnect state. Re-sending after a fresh
                # snapshot risks a duplicate `chat/turnStarted`.
                channel.pending.clear()
                continue
            channel.pending = [p for p in channel.pending if p.client_seq not in acked]

    # ── internals ────────────────────────────────────────────────────────────

    def _retire(self, channel: ChannelMirror, client_seq: Any) -> None:
        """Drop the pending entry this echo acknowledges, if there is one.

        Exact match, not cumulative. Cumulative ack bounds the queue when a host
        silently ignores an action on an unknown channel -- which the spec says
        it does, with no echo at all -- but it also drops a pending entry
        whenever a host echoes a later clientSeq first, which is legal. That
        trades a bounded leak for silent divergence.
        """
        if not isinstance(client_seq, int) or isinstance(client_seq, bool):
            return
        for index, entry in enumerate(channel.pending):
            if entry.client_seq == client_seq:
                del channel.pending[index]
                return

    def _check_gap(self, channel: ChannelMirror, server_seq: int) -> None:
        if self._gap_policy is GapPolicy.IGNORE or not server_seq or not channel.last_seq:
            return
        expected = channel.last_seq + 1
        if server_seq <= expected:
            return
        self._diagnose(SequenceGap(channel.uri, expected, server_seq))
        if self._gap_policy is GapPolicy.RESEED:
            self._stale.add(channel.uri)

    def _diagnose(self, diagnostic: Diagnostic) -> None:
        if self._on_diagnostic is not None:
            self._on_diagnostic(diagnostic)


def _server_seq(envelope: Mapping[str, Any]) -> int:
    raw = envelope.get("serverSeq")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


#: Re-exported so a caller binding channels does not need a second import.
REDUCER_NAMES: Final[frozenset[str]] = frozenset(REDUCERS)
