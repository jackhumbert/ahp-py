"""Structured events for an embedder that has to account for what happened.

Not the wire log, and not telemetry.

`--wire-log` is a **transcript**: every frame of every session, useful for
debugging a handshake and unusable as a retained record. Redaction and mode 0600
fixed its worst property, not its shape.

Upstream's OTLP channel points the other way -- at the *client* -- and its one
known consumer discards traces and metrics.

This is the third thing: a small number of decisions, retainable, with **no
conversation content by construction**. Identifiers and outcomes, never message
text. Every one of these is already a decision point inside the host, and
several are invisible to a provider because they happen at connection level
before any provider is involved.

Fire-and-forget: an exception from an embedder's sink is logged and swallowed,
the same rule the sequencer's subscription hooks follow, so an audit backend
cannot take the host down.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["AuditEvent", "AuditSink", "LoggingAuditSink"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEvent:
    """One decision the host made.

    ``kind`` is a stable string; ``detail`` carries identifiers only. Nothing
    here ever holds message text, tool input, file contents or a token -- if a
    field could contain any of those, it does not belong on this type.
    """

    kind: str
    #: The principal-ish identity the connection asserted, when there is one.
    #: Client-asserted and validated nowhere by the protocol -- an identifier,
    #: never an authenticator.
    client_id: str | None = None
    peer: str | None = None
    channel: str | None = None
    allowed: bool = True
    reason: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class AuditSink(Protocol):
    """Where audit events go. Supplied by the embedder, or not at all."""

    def record(self, event: AuditEvent) -> None: ...


class LoggingAuditSink:
    """Writes events to a logger. A default worth having, and not much more.

    Useful on its own for a single-user host, and useful as a shape to copy for
    an embedder wiring a real sink: the interesting property is that `record`
    never raises and never blocks.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger("agent_host_server.audit")

    def record(self, event: AuditEvent) -> None:
        self._log.info(
            "%s client=%s peer=%s channel=%s allowed=%s reason=%s %s",
            event.kind,
            event.client_id,
            event.peer,
            event.channel,
            event.allowed,
            event.reason,
            dict(event.detail) or "",
        )


def emit(sink: AuditSink | None, event: AuditEvent) -> None:
    """Hand an event to the sink, swallowing anything it throws.

    An audit backend that is down must not be able to refuse connections or drop
    sessions -- the record is a consequence of the host working, not a condition
    of it.
    """
    if sink is None:
        return
    try:
        sink.record(event)
    except Exception:
        _log.exception("audit sink failed on %s", event.kind)
