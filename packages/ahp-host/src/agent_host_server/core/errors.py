"""JSON-RPC and AHP errors.

Uses the spec's codes only -- never a parallel taxonomy. Codes come from the
vendored ``types/common/errors.ts``, not from ``errors.schema.json``, which is
missing ``-32011 Conflict`` because its generator hardcodes the enum.
"""

from __future__ import annotations

import errno
from typing import Any, Final

from agent_host_server.types import AHP_ERROR_CODES, JSON_RPC_ERROR_CODES

#: Re-exported: this is the module that raises with these, so it is where a
#: caller looks for them. Explicit, or a strict checker refuses the import.
__all__ = ["AHP_ERROR_CODES", "JSON_RPC_ERROR_CODES", "AhpError"]

__all__ = [
    "EACCES",
    "ELOOP",
    "EMLINK",
    "EPERM",
    "AhpError",
    "already_exists",
    "internal_error",
    "invalid_params",
    "method_not_found",
    "provider_not_found",
    "session_not_found",
    "unsupported_protocol_version",
]


class AhpError(Exception):
    """An error destined for a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data

    def to_json(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


#: `errno` values the resource jail has to distinguish. Imported here rather
#: than in `resources.py` so the numbers sit next to the error codes they map
#: onto, and so a platform without one of them fails at import rather than at
#: the first traversal.
ELOOP: Final = errno.ELOOP
EMLINK: Final = errno.EMLINK
EACCES: Final = errno.EACCES
EPERM: Final = errno.EPERM


def method_not_found(method: str) -> AhpError:
    """The honest answer for anything v0.1 does not implement.

    There is no AHP "not supported" code, and the protocol has no server
    capability object, so this error *is* how a host declines a feature.
    """
    return AhpError(JSON_RPC_ERROR_CODES["MethodNotFound"], f"Method not found: {method}")


def invalid_params(detail: str) -> AhpError:
    return AhpError(JSON_RPC_ERROR_CODES["InvalidParams"], f"Invalid params: {detail}")


def internal_error(detail: str) -> AhpError:
    return AhpError(JSON_RPC_ERROR_CODES["InternalError"], detail)


def unsupported_protocol_version(supported: tuple[str, ...]) -> AhpError:
    """-32005. The `data` payload lets a client explain the mismatch to the user.

    We deliberately omit `_meta.vscodeUpgradeMethod`: that is for hosts spawned
    by the VS Code CLI, and upstream states servers without a managing CLI omit
    it.
    """
    return AhpError(
        AHP_ERROR_CODES["UnsupportedProtocolVersion"],
        "No mutually supported protocol version",
        {"supportedProtocolVersions": list(supported)},
    )


def provider_not_found(provider: str) -> AhpError:
    """No agent serves this provider id.

    The protocol has a dedicated code for it, and answering anything else --
    including success -- is worse than it sounds: the client groups its session
    list BY provider, so a session accepted under a name no agent answers to
    files itself under an agent that does not exist while being served by the
    default one.
    """
    return AhpError(AHP_ERROR_CODES["ProviderNotFound"], f"No agent for provider: {provider}")


def session_not_found(uri: str) -> AhpError:
    return AhpError(AHP_ERROR_CODES["SessionNotFound"], f"No such session: {uri}")


def already_exists(uri: str) -> AhpError:
    return AhpError(AHP_ERROR_CODES["SessionAlreadyExists"], f"Session already exists: {uri}")
