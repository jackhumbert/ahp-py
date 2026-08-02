"""JSON-RPC and AHP errors.

Uses the spec's codes only -- never a parallel taxonomy. Codes come from the
vendored ``types/common/errors.ts``, not from ``errors.schema.json``, which is
missing ``-32011 Conflict`` because its generator hardcodes the enum.

Both directions live here because both peers need both. A host builds an
:class:`AhpError` and serialises it with :meth:`AhpError.to_json`; a client
receives one and rebuilds it with :func:`from_json`. Keeping the pair together
is what stops the two ends drifting into separate taxonomies for the same wire
codes.
"""

from __future__ import annotations

import errno
from collections.abc import Mapping
from typing import Any, Final

from agent_host_protocol.types import AHP_ERROR_CODES, JSON_RPC_ERROR_CODES

__all__ = [
    "EACCES",
    "ELOOP",
    "EMLINK",
    "EPERM",
    "AhpError",
    "already_exists",
    "from_json",
    "internal_error",
    "invalid_params",
    "method_not_found",
    "provider_not_found",
    "session_not_found",
    "unsupported_protocol_version",
]


class AhpError(Exception):
    """An error carried by a JSON-RPC error response, in either direction."""

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


def from_json(error: Mapping[str, Any]) -> AhpError:
    """Rebuild an :class:`AhpError` from a received ``error`` member.

    **Any** integer code is accepted, including one this build has never heard
    of. Two reasons, both observed rather than hypothetical: upstream's own
    ``errors.schema.json`` omits ``-32011 Conflict``, so validating against the
    published enum would reject a code the spec defines; and third-party hosts
    ship their own maps -- ``@wyrd-company/ahp-server`` answers a missing
    session with ``-32008 NotFound`` and does not define ``-32001`` at all.
    A client that raises on an unrecognised code turns someone else's extension
    into a crash.

    A malformed member -- no ``code``, or a non-integer one -- becomes
    ``InternalError``, because there is no honest way to attribute it and
    dropping it would leave the caller waiting on a request that already failed.
    """
    code = error.get("code")
    # `bool` is an `int` in Python and JSON `true` is not an error code.
    if not isinstance(code, int) or isinstance(code, bool):
        return AhpError(
            JSON_RPC_ERROR_CODES["InternalError"],
            f"Malformed JSON-RPC error member: {error!r}",
        )
    message = error.get("message")
    if not isinstance(message, str):
        message = ""
    # Absent and explicit `null` are the same thing here: the spec makes `data`
    # optional and no code assigns meaning to a null one.
    return AhpError(code, message, error.get("data"))


#: `errno` values a rooted-filesystem `resource*` implementation has to
#: distinguish. They live next to the error codes they map onto, and importing
#: them here means a platform missing one fails at import rather than at the
#: first path traversal. Both peers need them: the family is symmetrical, so a
#: client serving `resourceRead` back to a host walks the same jail.
ELOOP: Final = errno.ELOOP
EMLINK: Final = errno.EMLINK
EACCES: Final = errno.EACCES
EPERM: Final = errno.EPERM


def method_not_found(method: str) -> AhpError:
    """The honest answer for anything this peer does not implement.

    There is no AHP "not supported" code and no server capability object, so
    this error *is* how a peer declines a feature -- in either direction. A
    client with no handler for an inbound ``resource*`` request answers with it
    too, which is what stops the host leaking a pending request.
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
