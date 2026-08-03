"""What this client raises, and why each one is its own type.

Five families, mirroring the reference clients' taxonomy so a reader porting
from TypeScript finds what they expect:

* :class:`TransportError` -- the stream failed. Distinguishes a clean close from
  an I/O fault from an undecodable frame, because the supervisor above decides
  shutdown-versus-reconnect from exactly that.
* :class:`RpcError` -- the peer answered with an error. Subclassed per spec code
  so ``except SessionNotFound`` is possible; catching ``RpcError`` always works.
* :class:`RequestTimeout` -- nothing answered. Deliberately *not* an
  ``RpcError``: no peer error occurred, the wait elapsed, and callers commonly
  retry these where they would not retry a refusal.
* :class:`ClientClosed` -- we shut down while the call was in flight.
* :class:`ProtocolViolation` -- the peer broke a MUST. Rare, and always
  something a conformance bug report can be written from.

Everything derives from :class:`AhpClientError`, so one ``except`` catches the
whole library.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from agent_host_protocol.types import AHP_ERROR_CODES, JSON_RPC_ERROR_CODES

__all__ = [
    "AhpClientError",
    "AlreadyExists",
    "AuthRequired",
    "ClientClosed",
    "Conflict",
    "ContentNotFound",
    "InvalidArgument",
    "InvalidParams",
    "MethodNotFound",
    "NotFound",
    "PermissionDenied",
    "ProtocolVersionError",
    "ProtocolViolation",
    "ProviderNotFound",
    "RequestTimeout",
    "RpcError",
    "SessionAlreadyExists",
    "SessionNotFound",
    "TransportError",
    "TurnInProgress",
    "UnsupportedProtocolVersion",
    "is_session_gone",
    "rpc_error_from",
]


class AhpClientError(Exception):
    """Base for everything this library raises."""


class InvalidArgument(AhpClientError, ValueError):
    """A caller's argument cannot become a valid wire value.

    Both bases, on purpose. It **is** a ``ValueError`` -- that is what a bad
    argument is in Python, and it is what the action constructors raised before
    this class existed -- and it is an :class:`AhpClientError`, because this
    module's promise is that one ``except`` catches the whole library and the
    argument guards were the hole in it. The changeset review gate is the case
    that made it matter: the documented handler is ``except AhpClientError``,
    which caught the capability refusal and missed the empty-batch refusal one
    line away.
    """


# ── transport ────────────────────────────────────────────────────────────────

TransportErrorKind = Literal["closed", "io", "protocol", "rejected"]


class TransportError(AhpClientError):
    """The underlying stream failed.

    ``kind`` matters to the supervisor, not just to logging. A ``closed`` from a
    clean peer shutdown and an ``io`` mid-frame are the difference between "the
    host went away on purpose" and "the network blinked". ``rejected`` is the
    third case and the one that used to be flattened into ``io``: the handshake
    was *answered*, and the answer was no.

    ``status`` carries the HTTP status when a proxy refused the upgrade. Without
    it a 401 is indistinguishable from a connection reset, and a supervisor with
    no way to tell them apart retries an expired credential forever -- one doomed
    handshake per backoff interval against the very proxy rejecting it. A status
    code discloses nothing; the credential is redacted separately.
    """

    def __init__(
        self,
        kind: TransportErrorKind,
        message: str,
        *,
        status: int | None = None,
        close_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind: Final = kind
        #: HTTP status from a refused WebSocket upgrade, when there was one.
        self.status: Final = status
        #: WebSocket close code, when the peer closed rather than refused.
        self.close_code: Final = close_code


# ── lifecycle ────────────────────────────────────────────────────────────────


class ClientClosed(AhpClientError):
    """The client shut down while this call was in flight, or before it started."""

    def __init__(self, message: str = "client is closed") -> None:
        super().__init__(message)


class RequestTimeout(AhpClientError):
    """No response arrived within the deadline.

    Not an :class:`RpcError`: the peer did not refuse anything, and the
    distinction is what tells a caller whether retrying is sensible.
    """

    def __init__(self, method: str, timeout: float) -> None:
        super().__init__(f"request {method!r} timed out after {timeout}s")
        self.method: Final = method
        self.timeout: Final = timeout


class ProtocolViolation(AhpClientError):
    """A peer broke a documented MUST.

    Kept separate from :class:`RpcError` because a refusal is normal traffic and
    this is a bug in the other implementation.
    """


class ProtocolVersionError(ProtocolViolation):
    """The host answered ``initialize`` with a version we did not offer.

    The reference client does not check this and will proceed on a version it
    never offered -- measured, not assumed. See ADR 0005.
    """

    def __init__(self, negotiated: str, offered: Sequence[str]) -> None:
        super().__init__(
            f"host negotiated protocol version {negotiated!r}, "
            f"which is not among the offered {list(offered)!r}"
        )
        self.negotiated: Final = negotiated
        self.offered: Final = tuple(offered)


# ── rpc ──────────────────────────────────────────────────────────────────────


class RpcError(AhpClientError):
    """The peer answered with a JSON-RPC error member."""

    #: Set on subclasses; ``None`` on the base, which carries unmapped codes.
    code: int | None = None

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class SessionNotFound(RpcError):
    """-32001."""


class ProviderNotFound(RpcError):
    """-32002."""


class SessionAlreadyExists(RpcError):
    """-32003."""


class TurnInProgress(RpcError):
    """-32004."""


class UnsupportedProtocolVersion(RpcError):
    """-32005. ``data.supportedVersions`` explains the mismatch.

    Entries MAY be SemVer *range* constraints (``">=0.1.0 <0.3.0"``, ``"^0.2.0"``),
    not just exact versions, so they are surfaced verbatim rather than parsed.
    """

    @property
    def supported_versions(self) -> tuple[str, ...]:
        if isinstance(self.data, Mapping):
            # `supportedVersions` per errors.schema.json and errors.ts:157. The
            # longer spelling is not in the spec, but the shared package's own
            # error helper emitted it, so it is read as a fallback rather than
            # letting the one frame that explains a handshake failure parse to
            # nothing.
            raw = self.data.get("supportedVersions")
            if raw is None:
                raw = self.data.get("supportedProtocolVersions")
            if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
                return tuple(str(v) for v in raw)
        return ()


class ContentNotFound(RpcError):
    """-32006."""


class AuthRequired(RpcError):
    """-32007. MAY be returned from **any** command, not just session creation."""

    @property
    def resources(self) -> tuple[Mapping[str, Any], ...]:
        """``ProtectedResourceMetadata`` entries, RFC 9728, left snake_case.

        Nothing in this library renames wire fields, so these are handed back
        exactly as they arrived.
        """
        if isinstance(self.data, Mapping):
            raw = self.data.get("resources")
            if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
                return tuple(r for r in raw if isinstance(r, Mapping))
        return ()


class NotFound(RpcError):
    """-32008."""


class PermissionDenied(RpcError):
    """-32009."""


class AlreadyExists(RpcError):
    """-32010."""


class Conflict(RpcError):
    """-32011. Absent from upstream's published ``errors.schema.json``."""


class InvalidParams(RpcError):
    """-32602 -- the request was malformed.

    Distinct from :class:`NotFound`, and the distinction is actionable: -32008
    says the URI was well-formed and the resource was absent, so the caller
    should stop asking for it; -32602 says the caller left out a required
    param and should send a different frame.
    """


class MethodNotFound(RpcError):
    """-32601 -- "the peer does not implement this".

    AHP has no server capability object, so this is how a host declines a
    feature. It is a normal answer, not a fault.
    """


_BY_CODE: Final[dict[int, type[RpcError]]] = {
    AHP_ERROR_CODES["SessionNotFound"]: SessionNotFound,
    AHP_ERROR_CODES["ProviderNotFound"]: ProviderNotFound,
    AHP_ERROR_CODES["SessionAlreadyExists"]: SessionAlreadyExists,
    AHP_ERROR_CODES["TurnInProgress"]: TurnInProgress,
    AHP_ERROR_CODES["UnsupportedProtocolVersion"]: UnsupportedProtocolVersion,
    AHP_ERROR_CODES["ContentNotFound"]: ContentNotFound,
    AHP_ERROR_CODES["AuthRequired"]: AuthRequired,
    AHP_ERROR_CODES["NotFound"]: NotFound,
    AHP_ERROR_CODES["PermissionDenied"]: PermissionDenied,
    AHP_ERROR_CODES["AlreadyExists"]: AlreadyExists,
    AHP_ERROR_CODES["Conflict"]: Conflict,
    JSON_RPC_ERROR_CODES["MethodNotFound"]: MethodNotFound,
    JSON_RPC_ERROR_CODES["InvalidParams"]: InvalidParams,
}


def rpc_error_from(error: Mapping[str, Any]) -> RpcError:
    """Build the most specific :class:`RpcError` for a received error member.

    An unmapped code yields a plain :class:`RpcError` rather than raising.
    Upstream's own schema omits -32011, and third-party hosts ship their own
    maps, so treating an unrecognised code as a fault would turn someone else's
    extension into a crash.
    """
    raw = error.get("code")
    # `bool` is an `int` in Python; JSON `true` is not an error code.
    code = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    message = error.get("message")
    if not isinstance(message, str):
        message = ""
    data = error.get("data")
    if code is None:
        return RpcError(
            JSON_RPC_ERROR_CODES["InternalError"],
            f"malformed JSON-RPC error member: {error!r}",
            data,
        )
    return _BY_CODE.get(code, RpcError)(code, message, data)


def is_session_gone(error: BaseException) -> bool:
    """Whether an error means "that session no longer exists; make a new one".

    Two codes mean it in the wild and they must be handled in one place.
    ``-32001 SessionNotFound`` is the specified answer, but the third-party
    ``@wyrd-company/ahp-server`` defines ``NotFound = -32008`` and does not
    define ``-32001`` at all, so a client keyed only on the specified code
    retries forever against a session that is never coming back.
    """
    return isinstance(error, RpcError) and error.code in {
        AHP_ERROR_CODES["SessionNotFound"],
        AHP_ERROR_CODES["NotFound"],
    }
