"""Reconnect backoff.

Ported from the TypeScript and Rust supervisors. **Go is not a model**: it never
issues `reconnect` at all, and its `maxAttempts == 0` means *unlimited* --
inverted from every other implementation, so a policy copied from it retries
forever exactly where the author meant "do not retry".
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from agent_host_protocol.types import AHP_ERROR_CODES

from agent_host_client.client.errors import RpcError, TransportError

__all__ = [
    "Backoff",
    "ReconnectPolicy",
    "default_should_retry",
    "disabled_policy",
    "exponential_policy",
    "immediate_forever_policy",
    "retry_everything",
]

#: HTTP statuses on a refused upgrade that will not improve by waiting.
_REJECTED_STATUSES: frozenset[int] = frozenset({401, 403})


def default_should_retry(failure: BaseException) -> bool:
    """Whether a failed attempt is worth repeating.

    Declines the three unambiguous permanent refusals and retries everything
    else, because "the host was restarting" is far more common than any of them
    and guessing wrong in that direction costs a connection that would have come
    back.

    * **HTTP 401 / 403 on the upgrade.** An identity-aware proxy is the standard
      deployment shape off loopback, and per-user tokens expire. Retrying is not
      merely futile -- it is one doomed handshake per user per backoff interval
      against the proxy already rejecting them, while the user sees a
      disconnected client and nothing anywhere says "re-authenticate".
    * **A 1008 policy-violation close.** The peer refused this client rather
      than failing.
    * **`-32005 UnsupportedProtocolVersion`.** A permanent disagreement about
      the wire. No amount of waiting introduces a version both ends speak.

    Everything else -- including a plain `io` failure, an abnormal close, and
    any other RPC error -- is transient until proven otherwise.
    """
    if isinstance(failure, TransportError):
        if failure.kind != "rejected":
            return True
        if failure.status is not None:
            return failure.status not in _REJECTED_STATUSES
        return False
    if isinstance(failure, RpcError):
        return failure.code != AHP_ERROR_CODES["UnsupportedProtocolVersion"]
    return True


def retry_everything(_failure: BaseException) -> bool:
    """Treat every failure as transient -- the behaviour before classification.

    Here so "go back to how it was" is one named argument rather than a lambda
    somebody has to reverse-engineer.
    """
    return True


@dataclass(frozen=True, slots=True)
class Backoff:
    """Delay schedule between attempts."""

    kind: Literal["immediate", "constant", "exponential"] = "exponential"
    initial: float = 0.25
    maximum: float = 30.0
    multiplier: float = 2.0

    def delay_for(self, attempt: int) -> float:
        """Seconds before the *attempt*-th retry. One-based."""
        safe = max(1, attempt)
        if self.kind == "immediate":
            return 0.0
        if self.kind == "constant":
            return max(0.0, self.initial)
        multiplier = max(1.0, self.multiplier)
        return min(max(0.0, self.initial * multiplier ** (safe - 1)), self.maximum)


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    backoff: Backoff = Backoff()
    #: Delay is sampled uniformly from ``[d*(1-j), d*(1+j)]``. Clamped to [0, 1].
    jitter: float = 0.25
    #: ``None`` retries forever; ``0`` disables reconnect entirely. This is the
    #: TypeScript/Rust polarity, and it is the opposite of Go's.
    max_attempts: int | None = None
    reset_on_success: bool = True
    #: Classifies a failure as worth repeating. The default declines three
    #: permanent refusals; pass :func:`retry_everything` for the behaviour
    #: before classification existed, or supply your own predicate.
    #:
    #: This is separate from :attr:`max_attempts` on purpose. That decides *how
    #: many times*; this decides *whether at all*. A finite attempt budget is
    #: not a substitute -- it still burns every attempt against a credential
    #: that will never be accepted, and it ends in the same terminal state a
    #: single classification would have reached immediately.
    should_retry: Callable[[BaseException], bool] = field(default=default_should_retry)

    def exhausted(self, attempt: int) -> bool:
        return self.max_attempts is not None and attempt > self.max_attempts

    def delay_with_jitter(self, attempt: int, sample: float | None = None) -> float:
        """Delay with jitter applied.

        *sample* is injectable so a test can drive the schedule deterministically
        rather than sleeping through a real exponential curve.
        """
        base = self.backoff.delay_for(attempt)
        jitter = min(1.0, max(0.0, self.jitter))
        if jitter <= 0 or base == 0:
            return base
        drawn = random.random() if sample is None else min(1.0, max(0.0, sample))
        return max(0.0, base * (1 + (drawn * 2 - 1) * jitter))


def disabled_policy() -> ReconnectPolicy:
    """Never reconnect. One failure is terminal."""
    return ReconnectPolicy(backoff=Backoff("immediate"), jitter=0.0, max_attempts=0)


def immediate_forever_policy() -> ReconnectPolicy:
    """Retry instantly, forever. For tests; not for production."""
    return ReconnectPolicy(backoff=Backoff("immediate"), jitter=0.0, max_attempts=None)


def exponential_policy() -> ReconnectPolicy:
    """250 ms to 30 s, doubling, 25% jitter, forever. The default."""
    return ReconnectPolicy()
