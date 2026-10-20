"""Usage scope marker: a `TurnSink.usage` report that is one model request, not the turn.

This is an ahp-py extension carried in `_meta` under :data:`USAGE_META_KEY`. A provider that
reports usage per model request (rather than once for the turn) tags each report with
:func:`request_usage_meta`; a reader that sums the turn's usage can then tell the two apart.
Readers that do not know the key may treat the report as an ordinary one.

Everything here is pure and stdlib-only. Nothing raises on peer data.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = ["REQUEST_SCOPE", "USAGE_META_KEY", "is_request_scoped", "request_usage_meta"]

USAGE_META_KEY: Final = "ahp-py/usage"
REQUEST_SCOPE: Final = "request"


def request_usage_meta() -> dict[str, Any]:
    """The `_meta` that marks a usage report as one model request's usage."""
    return {USAGE_META_KEY: {"scope": REQUEST_SCOPE}}


def is_request_scoped(meta: Any) -> bool:
    """True only when *meta* is a mapping whose usage marker says the scope is a request."""
    if not isinstance(meta, Mapping):
        return False
    marker = meta.get(USAGE_META_KEY)
    return isinstance(marker, Mapping) and marker.get("scope") == REQUEST_SCOPE
