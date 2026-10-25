"""The shared numeric coercer: no bools, no NaN or infinity, > 0 for counts and money."""

from __future__ import annotations

import math
from typing import Any

import pytest

from ahp_host_flow.engine.numbers import is_number, money, whole

BAD: tuple[Any, ...] = (True, False, math.nan, math.inf, -math.inf, "3", None, [1])


@pytest.mark.parametrize("value", BAD)
def test_engine_numbers_reject(value: Any) -> None:
    assert whole(value) == 0
    assert money(value) == 0.0
    assert not is_number(value)


def test_engine_numbers_accept() -> None:
    assert (whole(3), whole(0), whole(-2), whole(2.5)) == (3, 0, 0, 0)
    assert (money(2), money(0.25), money(0), money(-1.0)) == (2.0, 0.25, 0.0, 0.0)
    assert is_number(0)
    assert is_number(-1.5)
