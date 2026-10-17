"""Output-token budget."""

import math
from typing import Any

import pytest

from ahp_host_flow.engine import Budget


def test_budget_unlimited() -> None:
    budget = Budget()
    assert budget.total is None
    assert budget.remaining == math.inf
    budget.add({"output_tokens": 10**9})
    assert budget.remaining == math.inf
    assert not budget.exhausted


def test_budget_sums_output_tokens() -> None:
    budget = Budget(25)
    budget.add({"output_tokens": 10})
    assert budget.spent == 10
    assert budget.remaining == 15.0
    budget.add({"output_tokens": 10})
    assert budget.spent == 20
    assert not budget.exhausted
    budget.add({"output_tokens": 10})
    assert budget.spent == 30
    assert budget.remaining == 0.0
    assert budget.exhausted


def test_budget_zero_total_is_exhausted() -> None:
    assert Budget(0).exhausted


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": 5},
        {"output_tokens": True},
        {"output_tokens": -3},
        {"output_tokens": "7"},
        {"output_tokens": 2.5},
    ],
)
def test_budget_ignores_bad_usage(usage: dict[str, Any]) -> None:
    budget = Budget(100)
    assert budget.add(usage) == 0
    assert budget.spent == 0


@pytest.mark.parametrize("total", [-1, True, 1.5])
def test_budget_rejects_bad_total(total: Any) -> None:
    with pytest.raises(ValueError, match="budget total"):
        Budget(total)
