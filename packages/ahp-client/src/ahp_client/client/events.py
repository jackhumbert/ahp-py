"""What a subscription yields, and what the client reports about itself.

Three streams, deliberately separate:

* per-URI :class:`SubscriptionEvent` -- the mutation stream for one channel.
  Lossless (ADR 0002).
* :class:`ClientEvent` on ``events()`` -- the same events, tagged with their
  channel, fanned in across every channel. Bounded, drop-oldest.
* :class:`Diagnostic` on ``diagnostics()`` -- everything that went *wrong* but
  is not worth an exception: a dropped tap entry, a sequence gap, a rejected
  action, a frame we could not parse.

The third exists because every other client makes these invisible. A dropped
event, a gap and a rejection are all recoverable and none of them should stop a
turn -- but a consumer that cannot see them cannot tell a quiet host from a
broken one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, TypeAlias

from ahp_protocol.types import JsonObject

__all__ = [
    "ActionEvent",
    "ActionRejected",
    "AuthRequiredEvent",
    "ClientEvent",
    "ConnectionState",
    "Diagnostic",
    "DroppedEvents",
    "MalformedFrame",
    "OtlpEvent",
    "ProgressEvent",
    "SequenceGap",
    "SessionAdded",
    "SessionRemoved",
    "SessionSummaryChanged",
    "SubscriptionEvent",
    "UnknownResponse",
]


# ── subscription events ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ActionEvent:
    """An ``ActionEnvelope``: the write-ahead mutation stream."""

    envelope: JsonObject

    @property
    def channel(self) -> str:
        return str(self.envelope.get("channel", ""))

    @property
    def action(self) -> JsonObject:
        raw = self.envelope.get("action")
        return raw if isinstance(raw, dict) else {}

    @property
    def server_seq(self) -> int:
        raw = self.envelope.get("serverSeq")
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0

    @property
    def rejection_reason(self) -> str | None:
        raw = self.envelope.get("rejectionReason")
        return raw if isinstance(raw, str) else None


@dataclass(frozen=True, slots=True)
class SessionAdded:
    """``root/sessionAdded``."""

    params: JsonObject


@dataclass(frozen=True, slots=True)
class SessionRemoved:
    """``root/sessionRemoved``."""

    params: JsonObject


@dataclass(frozen=True, slots=True)
class SessionSummaryChanged:
    """``root/sessionSummaryChanged``."""

    params: JsonObject


@dataclass(frozen=True, slots=True)
class AuthRequiredEvent:
    """``auth/required``.

    Ephemeral and never replayed, which is why a reconnect has to re-check
    authentication rather than assume outstanding challenges survived.
    """

    params: JsonObject


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """``root/progress``.

    The TypeScript client drops this at a ``default:`` branch that reaches
    neither subscriptions nor ``events()``. It only ever fires if the client
    supplied a ``progressToken`` on ``createSession``, so surfacing it is half
    the work; minting the token is the other half.
    """

    params: JsonObject


@dataclass(frozen=True, slots=True)
class OtlpEvent:
    """``otlp/exportLogs`` | ``otlp/exportTraces`` | ``otlp/exportMetrics``.

    Surfaced but never subscribed to in 0.1.0 -- see ADR 0007. A consumer that
    expands the ``ahp-otlp://logs{?level}`` template itself will receive these;
    we do not claim telemetry support on the strength of that.
    """

    signal: Literal["logs", "traces", "metrics"]
    params: JsonObject


SubscriptionEvent: TypeAlias = (
    ActionEvent
    | SessionAdded
    | SessionRemoved
    | SessionSummaryChanged
    | AuthRequiredEvent
    | ProgressEvent
    | OtlpEvent
)


@dataclass(frozen=True, slots=True)
class ClientEvent:
    """A :class:`SubscriptionEvent` tagged with the channel it was scoped to."""

    channel: str
    event: SubscriptionEvent


# ── connection state ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConnectionState:
    """Where the single-shot client is in its one and only lifecycle.

    ``idle -> connected -> closing -> closed``, and never backwards. A client
    whose transport died is finished; reconnection means a new transport and
    therefore a new client (ADR 0003).
    """

    status: Literal["idle", "connected", "closing", "closed"]
    #: Set only on ``closed``; ``None`` means a deliberate shutdown.
    error: BaseException | None = None


# ── diagnostics ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SequenceGap:
    """``serverSeq`` skipped forward on a channel we are subscribed to.

    Not necessarily a fault: a client sees only its own channels, so its view of
    a host-global counter legitimately has holes. It *is* how a lost envelope
    looks, and no reference client reports it at all. See ADR 0005.
    """

    channel: str
    expected: int
    received: int


@dataclass(frozen=True, slots=True)
class ActionRejected:
    """Our own action came back with a ``rejectionReason``.

    The optimistic effect has been reverted by the time this is emitted.
    """

    channel: str
    client_seq: int
    reason: str
    action: JsonObject


@dataclass(frozen=True, slots=True)
class DroppedEvents:
    """A bounded fan-in tap evicted entries because its reader fell behind.

    Per-channel subscriptions never produce this -- they are lossless (ADR
    0002). Only ``events()`` and the host-event stream can.
    """

    stream: str
    count: int


@dataclass(frozen=True, slots=True)
class MalformedFrame:
    """An inbound frame could not be decoded. Logged, counted, not fatal.

    One bad frame must not kill unrelated in-flight requests. Past a limit the
    transport closes, because a peer streaming garbage is not recovering.
    """

    detail: str
    total: int


@dataclass(frozen=True, slots=True)
class UnknownResponse:
    """A response arrived for a request id we are not waiting on.

    Almost always a timeout that already fired, occasionally a peer bug. Cheap
    to report and impossible to diagnose without.
    """

    request_id: Any


Diagnostic: TypeAlias = (
    SequenceGap | ActionRejected | DroppedEvents | MalformedFrame | UnknownResponse
)

#: Notification method -> the wrapper it becomes. Handled centrally so the
#: "which notifications does this client surface?" question has one answer, and
#: so the parity matrix can read it rather than trusting prose. The TypeScript
#: client handles the first five and silently drops the rest.
NOTIFICATION_METHODS: Final[frozenset[str]] = frozenset(
    {
        "action",
        "root/sessionAdded",
        "root/sessionRemoved",
        "root/sessionSummaryChanged",
        "auth/required",
        "root/progress",
        "otlp/exportLogs",
        "otlp/exportTraces",
        "otlp/exportMetrics",
    }
)
