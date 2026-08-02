"""Reconnect backoff.

Ported from the TypeScript and Rust supervisors. **Go is not a model**: it never
issues `reconnect` at all, and its `maxAttempts == 0` means *unlimited* --
inverted from every other implementation, so a policy copied from it retries
forever exactly where the author meant "do not retry".
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "Backoff",
    "ReconnectPolicy",
    "disabled_policy",
    "exponential_policy",
    "immediate_forever_policy",
]


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
