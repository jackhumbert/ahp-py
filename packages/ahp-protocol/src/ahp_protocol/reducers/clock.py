"""The injectable clock the reducers need.

The AHP reducers are **not pure**: ``chatReducer`` stamps ``modifiedAt`` from the
wall clock in six places (``types/channels-chat/reducer.ts`` lines 218, 253, 359,
723, 779, 812). Every language port therefore injects a clock -- Go exposes
``nowProvider``, Kotlin and Swift a ``currentTimestampProvider``, Rust a
test-only thread-local -- and the shared fixture corpus is authored against a
frozen ``Date.now() === 9999``.

This is the single place a reducer may read time. ``AGENTS.md`` invariant 2.

**Since 0.9.0 no reducer reads it.** Upstream removed all six wall-clock reads
from ``chatReducer``: ``modifiedAt`` is now derived from action data -- a turn's
``startedAt``, plus its ``duration`` when it ends -- through
:func:`add_milliseconds_to_timestamp`. The injectable clock stays because hosts
and clients still stamp ``startedAt`` / ``createdAt`` with :func:`now_iso`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

__all__ = [
    "MOCK_NOW_MS",
    "add_milliseconds_to_timestamp",
    "frozen_clock",
    "now_iso",
    "now_ms",
    "parse_iso_ms",
    "set_clock",
    "to_iso",
]

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

    # Outside 0000-9999, `toISOString` switches to the six-digit signed form.
    year_text = f"{year:04d}" if 0 <= year <= 9999 else f"{'+' if year > 0 else '-'}{abs(year):06d}"
    return (
        f"{year_text}-{month:02d}-{day:02d}T"
        f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}Z"
    )


def now_iso() -> str:
    """``new Date(Date.now()).toISOString()`` -- what the chat reducer stamps."""
    return to_iso(now_ms())


#: `Date.parse`'s ISO 8601 subset: a date, optionally a time (seconds and a
#: fraction optional), optionally `Z` or a `+HH:MM` offset. Six-digit signed
#: years are ECMAScript's expanded form.
_ISO = re.compile(
    r"^(?P<year>[+-]\d{6}|\d{4})(?:-(?P<month>\d{2})(?:-(?P<day>\d{2}))?)?"
    r"(?:T(?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2})(?:\.(?P<fraction>\d+))?)?"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})?)?$"
)

#: `toISOString` throws past +/-8.64e15 ms (the ECMAScript time value range).
_MAX_TIME_MS = 8_640_000_000_000_000


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 in the proleptic Gregorian calendar."""
    year -= month <= 2
    era = year // 400  # Python floors, so no truncation correction is needed
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_iso_ms(timestamp: Any) -> int | None:
    """``Date.parse`` for the ISO 8601 forms the protocol uses; ``None`` for NaN.

    DIVERGENCE, deliberate: a date-time with no zone designator is *local*
    time in JavaScript, which makes the reference's answer depend on the host
    machine's timezone. It is read as UTC here, the only reproducible choice.
    Non-ISO strings JavaScript engines accept by their own heuristics are
    ``None`` -- they are outside the protocol's ``string (ISO 8601)`` fields.
    """
    if not isinstance(timestamp, str):
        return None
    match = _ISO.match(timestamp)
    if match is None:
        return None
    year = int(match["year"])
    if match["year"] == "-000000":
        return None  # ECMAScript forbids negative zero as an expanded year.
    month = int(match["month"] or 1)
    day = int(match["day"] or 1)
    hour = int(match["hour"] or 0)
    minute = int(match["minute"] or 0)
    second = int(match["second"] or 0)
    millis = int((match["fraction"] or "0")[:3].ljust(3, "0"))
    if not (1 <= month <= 12 and hour <= 24 and minute <= 59 and second <= 59):
        return None
    # V8 bounds the day by 31 in every month and lets `2024-02-30` roll over
    # into March; the civil-day arithmetic below rolls over the same way.
    if not 1 <= day <= 31:
        return None
    if hour == 24 and (minute or second or millis):
        return None
    offset = 0
    zone = match["zone"]
    if zone and zone != "Z":
        zone_hours, zone_minutes = int(zone[1:3]), int(zone[4:6])
        if zone_hours > 23 or zone_minutes > 59:
            return None
        offset = (zone_hours * 60 + zone_minutes) * 60_000 * (1 if zone[0] == "+" else -1)
    elapsed = ((hour * 60 + minute) * 60 + second) * 1000 + millis
    result = _days_from_civil(year, month, day) * 86_400_000 + elapsed - offset
    return result if abs(result) <= _MAX_TIME_MS else None


def add_milliseconds_to_timestamp(timestamp: Any, duration: Any) -> str | None:
    """``new Date(Date.parse(timestamp) + duration).toISOString()`` (0.9.0).

    ``None`` exactly where the reference throws -- ``toISOString`` raises a
    ``RangeError`` on an invalid date, which an unparseable timestamp or a
    non-finite duration produces. The caller decides what that means.
    """
    start = parse_iso_ms(timestamp)
    if start is None or isinstance(duration, bool) or not isinstance(duration, int | float):
        return None
    total = start + duration
    if total != total or abs(total) > _MAX_TIME_MS:  # NaN, or out of range
        return None
    # TimeClip: ToIntegerOrInfinity truncates toward zero.
    return to_iso(int(total))
