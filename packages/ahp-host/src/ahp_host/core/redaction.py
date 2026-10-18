"""Host-side plumbing for redaction after publishing (the ``ahp-py/redaction`` extension).

The reducer semantics live in :mod:`ahp_protocol.ext.redaction`; this module is
the small amount the host adds -- the error a bad target raises, the audit kind,
and how a peer's advertised extensions are read. See ADR 0007 for why a
redaction truncates replay rather than rewriting logged actions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from ahp_protocol.ext.redaction import REDACTION_CAPABILITY

__all__ = ["AUDIT_KIND", "RedactionError", "extensions_of", "redacts_live"]

#: The audit event kind a redaction records. Its detail names the target, the
#: time and the actor -- never the content that was removed.
AUDIT_KIND: Final = "chat.redacted"

#: Every ahp-py extension is namespaced; anything else in `_meta` is not ours.
_NAMESPACE: Final = "ahp-py/"


class RedactionError(ValueError):
    """``Host.redact`` was given a target that cannot be redacted.

    The message names the channel, turn and part and never any content, so it is
    safe to log and to show an operator.
    """


def extensions_of(params: Mapping[str, Any]) -> frozenset[str]:
    """The ahp-py extensions a peer advertised in ``params["_meta"]``.

    A presence flag, like ``ClientCapabilities``: the key counts when its value
    is an object (``{}``). Keys outside the ``ahp-py/`` namespace and
    non-object values are ignored, so a stranger's ``_meta`` cannot opt a
    connection in by accident.
    """
    meta = params.get("_meta")
    if not isinstance(meta, Mapping):
        return frozenset()
    return frozenset(
        key
        for key, value in meta.items()
        if isinstance(key, str) and key.startswith(_NAMESPACE) and isinstance(value, Mapping)
    )


def redacts_live(subscriber: object) -> bool:
    """Whether *subscriber* understands the redaction extension action.

    Anything else is never sent it (an unknown action must not reach a stock
    client) and is owed a snapshot instead.
    """
    return REDACTION_CAPABILITY in getattr(subscriber, "extensions", frozenset())
