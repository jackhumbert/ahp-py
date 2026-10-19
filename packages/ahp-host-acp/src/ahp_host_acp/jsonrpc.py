"""Deprecated: ACP's transport now lives in :mod:`ahp_acp`.

This module only re-exports the names it used to define, so existing imports
keep working. Import from `ahp_acp` (`ahp_acp.connection`, `ahp_acp.errors`)
instead.
"""

from __future__ import annotations

from ahp_acp.connection import (
    EXIT_GRACE,
    LINE_LIMIT,
    STDERR_TAIL,
    AcpConnection,
    NotificationHandler,
    RequestHandler,
    resolve_command,
)
from ahp_acp.errors import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    AcpError,
    AgentExitedError,
    MethodNotFoundError,
)

__all__ = [
    "EXIT_GRACE",
    "INTERNAL_ERROR",
    "LINE_LIMIT",
    "METHOD_NOT_FOUND",
    "STDERR_TAIL",
    "AcpConnection",
    "AcpError",
    "AgentExitedError",
    "MethodNotFoundError",
    "NotificationHandler",
    "RequestHandler",
    "resolve_command",
]
