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
* ``optimistic`` -- ``confirmed`` with ``pending`` replayed on top. Recomputed
  after every write and cached between them, so a foreign action rebases the
  pending queue for free while two reads with nothing in between agree.

Render ``optimistic``. Trust ``confirmed``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from ahp_protocol.channels import ROOT_URI, reducer_for_state
from ahp_protocol.reducers import REDUCERS, Reducer
from ahp_protocol.types import JsonObject

from ahp_client.client.events import ActionRejected, Diagnostic, SequenceGap

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
    #: An action the host refused and did not apply. Ours, whose optimistic
    #: effect is reverted, or another client's, which we must not apply either.
    REJECTED = "rejected"
    #: For a channel with no snapshot yet. Held, not dropped.
    BUFFERED = "buffered"
    #: At or below the channel's snapshot baseline; already accounted for.
    STALE = "stale"
    #: Not a channel we track.
    UNKNOWN_CHANNEL = "unknownChannel"


class GapPolicy(Enum):
    """What to do about a hole in ``serverSeq``.

    Measured against the **global** high-water mark, never against one
    channel's. ``serverSeq`` is a single host-global counter shared by every
    channel, so a channel's own numbers are never contiguous and a per-channel
    test calls ordinary interleaving a hole.

    Never fatal in any mode. A client sees only the channels it subscribed to,
    so its view of a host-global counter legitimately has holes -- raising would
    make the common case an error. See ADR 0005.
    """

    #: Reference-compatible: apply and say nothing.
    IGNORE = "ignore"
    #: Report on ``diagnostics()``, then apply. The default.
    WARN = "warn"
    #: Additionally mark every tracked channel stale so a caller can re-subscribe.
    RESEED = "reseed"


class PendingPolicy(Enum):
    """What happens to un-echoed dispatches across a reconnect.

    Three references disagree, which is why this is a named switch rather than a
    silent choice (ADR 0006b). ``docs/guide/reconciliation.md:81`` says clear in
    both arms; VS Code re-sends survivors on the replay arm; Swift re-sends
    always.

    Whatever a policy *keeps*, :meth:`StateMirror.on_reconnect` hands back to be
    re-sent. Keeping an entry without putting it back on the wire is the one
    outcome none of the three references describes and the only indefensible
    one: ``optimistic`` -- the state this library tells you to render -- would
    then show, forever, a turn the host has never heard of.
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
    #: Bumped by every write path that can change what ``optimistic`` returns.
    #: The cache key, not a wire sequence number.
    version: int = 0
    _optimistic_cache: Any = field(default=None, repr=False)
    _optimistic_version: int = field(default=-1, repr=False)

    @property
    def optimistic(self) -> Any:
        """``confirmed`` with ``pending`` replayed. Recomputed per write, not
        per read.

        Two guards ported from VS Code's ``_recomputeOptimistic``
        (agentSubscription.ts:509-526). Before the snapshot there is nothing to
        replay onto -- the reducers spread ``{**state}`` and raise ``TypeError``
        on ``None``, where the reference keeps its value ``undefined`` -- so a
        dispatch that raced the subscribe round trip stays pending without
        being reduced. And the replay is cached against :attr:`version`
        because the chat reducer stamps ``modifiedAt`` from the clock on every
        run: recomputed per read, two consecutive reads never compare equal
        and drift with the wall clock while nothing arrives, defeating the
        equality-based change detection the reference's compute-on-event model
        supports. A foreign action still rebases the queue for free -- it bumps
        the version, and the next read replays.
        """
        if self.confirmed is None:
            return None
        if not self.pending:
            return self.confirmed
        if self._optimistic_version != self.version:
            state = self.confirmed
            for entry in self.pending:
                state = self.reducer(state, entry.action)
            self._optimistic_cache = state
            self._optimistic_version = self.version
        return self._optimistic_cache


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
        self._loop_thread: int | None = None

    # ── registration ─────────────────────────────────────────────────────────

    def bind(self, uri: str, reducer_name: str) -> ChannelMirror:
        """Bind a reducer to a channel, by **name**, from the caller.

        The caller knows what it subscribed to. Nothing here infers a reducer
        from a URI scheme -- real clients mint URIs the scheme table does not
        describe, and a scheme-routed lookup silently binds nothing, which
        freezes state while actions keep arriving.
        """
        uri = _canonical_channel(uri)
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
        uri = _canonical_channel(uri)
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
        """Optimistic state -- what a UI renders.

        Asserted like the write paths: this read *reduces* whenever the
        pending queue is non-empty and dirty, and reduction reads the
        module-global clock, so ``asyncio.to_thread(render, mirror.state(u))``
        is exactly the silent off-loop hazard invariant 13 exists to make loud.
        """
        self._assert_single_threaded()
        channel = self._channels.get(_canonical_channel(uri))
        return None if channel is None else channel.optimistic

    def confirmed(self, uri: str) -> Any:
        """Server-acknowledged state -- what a decision is made on.

        Timestamps in particular should be read from here. The chat reducer
        stamps ``modifiedAt`` from the clock at turn end, so replaying a pending
        action produces a *different* stamp than the host's echo will; rendering
        the optimistic one guarantees a visible diff at the end of every turn.
        """
        channel = self._channels.get(_canonical_channel(uri))
        return None if channel is None else channel.confirmed

    def pending(self, uri: str) -> Sequence[PendingAction]:
        channel = self._channels.get(_canonical_channel(uri))
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
        uri = _canonical_channel(str(snapshot.get("resource", "")))
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
        channel.version += 1
        # `fromSeq` is a reading of the host-GLOBAL counter, so it also tells us
        # how far that counter has run. Without this a channel subscribed to
        # late -- baseline 50 while we have only seen 10 -- makes its own first
        # action look like a 40-envelope hole.
        self._last_server_seq = max(self._last_server_seq, from_seq)
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
        ``frozen_clock``. One cheap check on the hot path buys a loud failure
        instead.

        Invariant 13 names the *running loop's* thread as the authority, not
        whichever thread happened to call first: a pre-loop reader that won
        the pin would otherwise make the client's own read loop the "wrong"
        thread and take the connection down with the complaint. So a caller
        with a running loop adopts the pin, and the eventual ``RuntimeError``
        lands on the off-loop reader. Loop-less use (synchronous tests) keeps
        the plain first-caller pin.
        """
        current = threading.get_ident()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            if self._loop_thread is None:
                self._loop_thread = current
                self._thread = current
            elif self._loop_thread != current:
                raise RuntimeError(
                    "StateMirror was used from two event loops' threads; the "
                    "reducers read a module-global clock and must stay on one loop"
                )
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
        channel = self._channels.get(_canonical_channel(uri))
        if channel is None:
            return
        channel.pending.append(PendingAction(client_seq, dict(action)))
        channel.version += 1

    def apply(self, envelope: Mapping[str, Any]) -> ApplyOutcome:
        """Fold one ``ActionEnvelope`` in. Never raises on protocol data."""
        self._assert_single_threaded()
        # Canonicalised exactly as `apply_snapshot` is: the reference's
        # `_isRelevantEnvelope` matches the root channel through
        # `isAhpRootChannel` (agentSubscription.ts:232-234), so an envelope
        # spelling it `ahp-root:` must not fall out as `UNKNOWN_CHANNEL`.
        uri = _canonical_channel(str(envelope.get("channel", "")))
        channel = self._channels.get(_canonical_channel(uri))
        server_seq = _server_seq(envelope)
        # Snapshot the global mark before advancing it: it is what the gap check
        # measures against, and every envelope we see advances it -- including
        # ones for channels we buffer, discard as stale or do not track at all.
        previous_global = self._last_server_seq
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

        self._check_gap(channel, server_seq, previous_global)

        action = envelope.get("action")
        action = dict(action) if isinstance(action, Mapping) else {}
        origin = envelope.get("origin")
        origin = origin if isinstance(origin, Mapping) else None
        rejection = envelope.get("rejectionReason")
        # Absent and explicitly-null origin are the same thing: server-originated.
        own = origin is not None and origin.get("clientId") == self._client_id
        client_seq = origin.get("clientSeq") if origin is not None else None

        if isinstance(rejection, str):
            # `rejectionReason` is a property of the ENVELOPE, not of the
            # originator's copy of it: the host fans a refused action out to
            # every subscriber of the channel while leaving its own state
            # untouched. Reducing it because it came from someone else diverges
            # from the host permanently -- there is no later action that
            # corrects it, so a second client watching a refused
            # `terminal/claimed` or `chat/truncated` never recovers.
            if own:
                # Only the originator has an optimistic effect to revert, and
                # `ActionRejected` is documented as being about ours.
                self._retire(channel, client_seq)
                self._diagnose(ActionRejected(uri, client_seq or 0, rejection, action))
            # The host consumed this serverSeq for the rejection exactly as it
            # does for an applied action and logged it for replay, so the number
            # is accounted for. Leaving `last_seq` behind would make the next
            # envelope look like a hole and turn every rejection into a false
            # `SequenceGap` on a stream that exists to be trusted.
            if server_seq:
                channel.last_seq = server_seq
            channel.version += 1
            return ApplyOutcome.REJECTED

        if own:
            # Retire the matching entry, then apply. VS Code matches on an exact
            # clientSeq -- and still applies when nothing matches, which is the
            # arm every reimplementation of this leaves out.
            self._retire(channel, client_seq)
        else:
            self._promote_pending_turn_start(channel, action)

        channel.confirmed = channel.reducer(channel.confirmed, action)
        if server_seq:
            channel.last_seq = server_seq
        channel.version += 1
        return ApplyOutcome.APPLIED

    def on_reconnect(
        self,
        *,
        policy: PendingPolicy,
        arm: str,
        acknowledged: Iterable[int] = (),
    ) -> list[tuple[str, PendingAction]]:
        """Settle the pending queues after a reconnect; return what to re-send.

        *arm* is ``"replay"`` or ``"snapshot"``. The three reference
        implementations disagree about this; see :class:`PendingPolicy`.

        **Every surviving entry is returned, and the caller MUST put it back on
        the wire** with its original ``clientSeq`` so the host's echo still
        reconciles against it. A survivor that is neither re-sent nor cleared
        leaves ``optimistic`` showing an action the host never received, with
        nothing that can ever retire it -- the failure mode ADR 0006b's trade
        was chosen to avoid, not the one it accepted.

        Ordered by ``clientSeq`` across channels, so the host sees them in the
        order they were originally dispatched.
        """
        acked = set(acknowledged)
        resend: list[tuple[str, PendingAction]] = []
        for channel in self._channels.values():
            if policy is PendingPolicy.RESEND_ALL:
                resend.extend((channel.uri, entry) for entry in channel.pending)
                continue
            if policy is PendingPolicy.SPEC or arm == "snapshot":
                # Predicated on pre-disconnect state. Re-sending after a fresh
                # snapshot risks a duplicate `chat/turnStarted`.
                channel.pending.clear()
                channel.version += 1
                continue
            channel.pending = [p for p in channel.pending if p.client_seq not in acked]
            channel.version += 1
            resend.extend((channel.uri, entry) for entry in channel.pending)
        resend.sort(key=lambda item: item[1].client_seq)
        return resend

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

    def _check_gap(self, channel: ChannelMirror, server_seq: int, previous: int) -> None:
        """Measure the hole against the **global** mark, not the channel's.

        ``serverSeq`` is one host-global counter that every channel draws from,
        so per-channel contiguity is not a property the protocol provides:
        ``root@5, chat@6, root@7`` is ordinary traffic with nothing lost, and a
        channel-local test reports both steps as holes. Two subscribed channels
        are enough to make every reported gap a false positive, which costs the
        whole diagnostics stream its meaning -- it exists so a consumer can tell
        a quiet host from a broken one.

        Globally, what is left is exactly the two cases worth reporting: an
        envelope we never received, and traffic on a channel we did not
        subscribe to. ADR 0005 says the second is legal, which is why this warns
        rather than raises.

        *channel* is the one whose envelope revealed the hole, reported so a
        consumer has somewhere to look -- not a claim about which channel lost
        anything.

        A *previous* of zero is a real baseline, not the absence of one: this
        is only reachable after ``apply_snapshot`` set ``has_snapshot``, and a
        fresh host's snapshot legitimately reports ``fromSeq: 0``. Treating
        zero as "no baseline" made a hole in the very first envelopes -- seq 5
        arriving first -- the one gap this check could never see.
        """
        if self._gap_policy is GapPolicy.IGNORE or not server_seq:
            return
        expected = previous + 1
        if server_seq <= expected:
            return
        self._diagnose(SequenceGap(channel.uri, expected, server_seq))
        if self._gap_policy is GapPolicy.RESEED:
            # A global hole cannot be attributed: the lost envelope belonged to
            # whichever channel the host numbered it on, and that is precisely
            # the information the hole destroyed. Reseeding only the channel
            # that revealed it would leave the actual victim silently wrong.
            self._stale.update(self._channels)

    def _promote_pending_turn_start(
        self, channel: ChannelMirror, action: Mapping[str, Any]
    ) -> None:
        """Retire the pending ``chat/turnStarted`` a foreign terminal action closes.

        A port of ``_promotePendingTurnStartIfTerminal``
        (agentSubscription.ts:483-501): a backend-originated
        ``chat/turnComplete``, ``chat/turnCancelled`` or ``chat/error`` can
        arrive without ever echoing the ``turnStarted`` we dispatched -- no
        ``clientSeq``, so `_retire` never matches. Without the promotion the
        reducer's ``_end_turn`` no-ops (confirmed has no matching
        ``activeTurn``), the pending entry survives with nothing that can ever
        retire it, ``optimistic`` renders a stuck active turn forever, and the
        turn never reaches confirmed history. The start is applied to
        confirmed first so the terminal action closes a turn that exists.
        """
        if action.get("type") not in _TERMINAL_TURN_ACTIONS:
            return
        turn_id = action.get("turnId")
        for index, entry in enumerate(channel.pending):
            pending = entry.action
            if pending.get("type") != "chat/turnStarted" or pending.get("turnId") != turn_id:
                continue
            del channel.pending[index]
            confirmed = channel.confirmed
            active = confirmed.get("activeTurn") if isinstance(confirmed, Mapping) else None
            active_id = active.get("id") if isinstance(active, Mapping) else None
            if confirmed is not None and active_id != turn_id:
                channel.confirmed = channel.reducer(confirmed, pending)
            return

    def _diagnose(self, diagnostic: Diagnostic) -> None:
        if self._on_diagnostic is not None:
            self._on_diagnostic(diagnostic)


def _server_seq(envelope: Mapping[str, Any]) -> int:
    raw = envelope.get("serverSeq")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _canonical_channel(uri: str) -> str:
    """``ahp-root:`` and ``ahp-root://`` are one channel.

    VS Code matches the root channel by *scheme* -- ``isAhpRootChannel``
    (sessionState.ts:478-487), whose doc says to always prefer it over a direct
    ``=== ROOT_STATE_URI`` comparison -- because the authority-less form
    round-trips out of URI normalisation. An exact-string lookup drops that
    variant as ``UNKNOWN_CHANNEL`` and silently freezes root state against a
    host that normalises URIs. Scoped to the root scheme only: no other scheme
    is routed here (invariant 1).
    """
    return ROOT_URI if uri.startswith("ahp-root:") else uri


#: The chat actions that close a turn -- the trigger set of
#: ``_promotePendingTurnStartIfTerminal`` (agentSubscription.ts:490).
_TERMINAL_TURN_ACTIONS: Final[frozenset[str]] = frozenset(
    {"chat/turnComplete", "chat/turnCancelled", "chat/error"}
)


#: Re-exported so a caller binding channels does not need a second import.
REDUCER_NAMES: Final[frozenset[str]] = frozenset(REDUCERS)
