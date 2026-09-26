"""`add_milliseconds_to_timestamp` against the reference expression.

Since 0.9.0 the chat reducer derives `modifiedAt` from
`new Date(Date.parse(startedAt) + duration).toISOString()`. Every expectation
below was produced by that expression in Node (V8), not written by hand;
`None` is where it throws a `RangeError`.
"""

from __future__ import annotations

import pytest

from ahp_protocol.reducers.clock import add_milliseconds_to_timestamp

CASES = [
    ("2024-02-29T23:59:59.750-05:00", 500, "2024-03-01T05:00:00.250Z"),
    ("1970-01-01T00:00:00.000Z", 9999, "1970-01-01T00:00:09.999Z"),
    ("bogus", 1, None),
    ("2024-01-01", 0, "2024-01-01T00:00:00.000Z"),
    ("1969-12-31T23:59:59.999Z", 0.9, "1970-01-01T00:00:00.000Z"),
    ("-000001-01-01T00:00:00Z", 0, "-000001-01-01T00:00:00.000Z"),
    ("+010000-01-01T00:00:00Z", 0, "+010000-01-01T00:00:00.000Z"),
    ("2024-01-01T24:00:00Z", 0, "2024-01-02T00:00:00.000Z"),
    ("2024-02-30T00:00:00Z", 0, "2024-03-01T00:00:00.000Z"),
    ("2024-01-01T00:00:00.123456Z", 0, "2024-01-01T00:00:00.123Z"),
    ("2024-01-01T00:00Z", -1, "2023-12-31T23:59:59.999Z"),
    ("2024-06", 0, "2024-06-01T00:00:00.000Z"),
    ("2024", 5, "2024-01-01T00:00:00.005Z"),
    ("2023-02-29", 0, "2023-03-01T00:00:00.000Z"),
    ("0000-03-01T00:00:00Z", 0, "0000-03-01T00:00:00.000Z"),
    ("-271821-04-20T00:00:00Z", 0, "-271821-04-20T00:00:00.000Z"),
    ("-271821-04-19T00:00:00Z", 0, None),
    ("2024-01-01T00:00:00+14:00", 0, "2023-12-31T10:00:00.000Z"),
    ("1600-02-29T12:00:00Z", -1.5, "1600-02-29T11:59:59.999Z"),
]


@pytest.mark.parametrize(("timestamp", "duration", "expected"), CASES)
def test_matches_v8(timestamp: str, duration: float, expected: str | None) -> None:
    assert add_milliseconds_to_timestamp(timestamp, duration) == expected


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), "5", None, True])
def test_a_non_finite_or_non_number_duration_is_a_range_error(duration: object) -> None:
    """The reducer coerces with `Math.max` first; this helper takes the result."""
    assert add_milliseconds_to_timestamp("2024-01-01T00:00:00Z", duration) is None
