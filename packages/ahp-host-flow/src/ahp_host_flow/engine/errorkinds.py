"""Error classification: a worker turn's errorType to a (kind, transient) pair."""

from __future__ import annotations

import re
from typing import Final

from ahp_host_flow.engine.dispatcher import ErrorKind

#: A provider that fails to load.
PROVIDER_LOAD: Final = "flow.providerLoad"
#: A remote TurnFailed with an empty errorType (ahp-client synthesised it for a refused
#: turnStarted or a disposed session).
REMOTE_LOST: Final = "flow.remoteLost"
#: The remote turn produced nothing for idle_timeout.
REMOTE_IDLE: Final = "flow.remoteIdle"

# ahp-host-claude marks 408/429/5xx/529, rate_limit/overloaded/server_error resumable but
# does not pass the status through the sink, so 429 is read from Claude Code's error text.
RATE_LIMIT_TEXT: Final = re.compile(r"\b429\b|rate[ _-]?limit", re.IGNORECASE)

#: The rows of `classify`, first match wins, as (pattern, kind) for the README.
ERROR_TABLE: Final[tuple[tuple[str, str], ...]] = (
    ("acp.start", "unavailable"),
    ("acp.agentExited", "unavailable"),
    ("acp.refusal", "refused"),
    ("acp.max_tokens", "failed"),
    ("claude.exited", "unavailable"),
    ("claude.* (resumable, rate-limit text)", "rate_limited"),
    ("claude.* (resumable)", "unavailable"),
    ("claude.*", "failed"),
    (PROVIDER_LOAD, "unavailable"),
    (REMOTE_LOST, "unavailable"),
    (REMOTE_IDLE, "timeout"),
    ("anything else", "failed"),
)


def classify(
    error_type: str, *, resumable: bool = False, message: str = ""
) -> tuple[ErrorKind, bool]:
    """The (kind, transient) of a failed turn."""
    if error_type in ("acp.start", "acp.agentExited", "claude.exited"):
        return "unavailable", True
    if error_type == "acp.refusal":
        return "refused", False
    if error_type == "acp.max_tokens":
        return "failed", False
    if error_type.startswith("claude."):
        if not resumable:
            return "failed", False
        if RATE_LIMIT_TEXT.search(message):
            return "rate_limited", True
        return "unavailable", True
    if error_type == PROVIDER_LOAD:
        return "unavailable", False
    if error_type == REMOTE_LOST:
        return "unavailable", True
    if error_type == REMOTE_IDLE:
        return "timeout", True
    return "failed", resumable
