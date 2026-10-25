"""The numeric coercion the usage counters share: no bools, no NaN or infinity."""

from __future__ import annotations

import math
from typing import Any


def is_number(value: Any) -> bool:
    """True for a finite int or float that is not a bool."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def whole(value: Any) -> int:
    """*value* when it is an int > 0 (not a bool), else 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def money(value: Any) -> float:
    """*value* as a float when it is a finite number > 0 (not a bool), else 0.0."""
    return float(value) if is_number(value) and value > 0 else 0.0
