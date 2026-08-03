"""The injectable clock the reducers need.

The AHP reducers are **not pure**: ``chatReducer`` stamps ``modifiedAt`` from the
wall clock in six places (``types/channels-chat/reducer.ts`` lines 218, 253, 359,
723, 779, 812). Every language port therefore injects a clock -- Go exposes
``nowProvider``, Kotlin and Swift a ``currentTimestampProvider``, Rust a
test-only thread-local -- and the shared fixture corpus is authored against a
frozen ``Date.now() === 9999``.

This is the single place a reducer may read time. ``AGENTS.md`` invariant 2.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

__all__ = ["MOCK_NOW_MS", "frozen_clock", "now_iso", "now_ms", "set_clock", "to_iso"]

#: What the fixture corpus is authored against: `Date.now() === 9999`, which
#: stamps as "1970-01-01T00:00:09.999Z". 28 fixtures assert that literal.
MOCK_NOW_MS = 9999

_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _system_now_ms() -> int:
    # Imported lazily so the module-level import graph of `reducers` stays free
    # of anything a reducer could reach for by accident.
    import time

    return int(time.time() * 1000)


_clock: Callable[[], int] = _system_now_ms


def now_ms() -> int:
    """Current epoch milliseconds, from the injected clock."""
    return _clock()


def set_clock(clock: Callable[[], int]) -> Callable[[], int]:
    """Replace the clock; returns the previous one."""
    global _clock
    previous = _clock
    _clock = clock
    return previous


@contextmanager
def frozen_clock(millis: int = MOCK_NOW_MS) -> Iterator[None]:
    """Pin the clock, as every language's fixture harness does."""
    previous = set_clock(lambda: millis)
    try:
        yield
    finally:
        set_clock(previous)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def to_iso(millis: int) -> str:
    """Format epoch millis exactly as JavaScript's ``Date.prototype.toISOString``.

    ``datetime.isoformat()`` will not do: it emits six-digit microseconds and
    ``+00:00`` where the protocol needs three-digit milliseconds and ``Z``.
    Computed by hand (civil-from-days, mirroring the Rust port) so the result
    does not depend on platform ``strftime`` behaviour or on any local timezone.
    """
    days, rem = divmod(millis, 86_400_000)
    if rem < 0:  # Python's divmod already floors, but be explicit for negatives.
        rem += 86_400_000
        days -= 1
    hours, rem = divmod(rem, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, milliseconds = divmod(rem, 1000)

    year = 1970
    while True:
        year_days = 366 if _is_leap(year) else 365
        if days < year_days:
            break
        days -= year_days
        year += 1
    while days < 0:
        year -= 1
        days += 366 if _is_leap(year) else 365

    month = 1
    for index, length in enumerate(_DAYS_IN_MONTH):
        if index == 1 and _is_leap(year):
            length += 1
        if days < length:
            break
        days -= length
        month += 1
    day = days + 1

    return (
        f"{year:04d}-{month:02d}-{day:02d}T"
        f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}Z"
    )


def now_iso() -> str:
    """``new Date(Date.now()).toISOString()`` -- what the chat reducer stamps."""
    return to_iso(now_ms())
