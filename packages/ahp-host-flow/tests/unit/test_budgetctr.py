"""Named budget counters on the Scheduler: input, cost, output message and options (M16)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ahp_host_flow.engine import (
    AgentCall,
    BudgetExhaustedError,
    RunOptions,
    Scheduler,
)

from ..limits_fakes import UsageDispatcher  # noqa: TID252


async def _run(s: Scheduler, n: int, prompt: str) -> Any:
    call = AgentCall(ordinal=n, prompt=prompt)
    s.admit(call)
    return await asyncio.wait_for(s.run(call), 10)


async def test_budgetctr_input_counter() -> None:
    s = Scheduler(UsageDispatcher(), RunOptions(input_budget=100))
    await _run(s, 0, "use:40:1:20:0")
    await _run(s, 1, "use:40:1:20:0")
    assert s.budget.value("input_tokens") == 120
    with pytest.raises(BudgetExhaustedError) as info:
        await _run(s, 2, "use:40:1:20:0")
    assert str(info.value) == "input budget exhausted: 120 of 100 input tokens spent"


async def test_budgetctr_count_cache_false() -> None:
    s = Scheduler(UsageDispatcher(), RunOptions(input_budget=100, count_cache=False))
    await _run(s, 0, "use:40:1:20:0")
    await _run(s, 1, "use:40:1:20:0")
    assert s.budget.value("input_tokens") == 80
    assert await _run(s, 2, "use:40:1:20:0") == "use:40:1:20:0"


async def test_budgetctr_cost_counter() -> None:
    s = Scheduler(UsageDispatcher(), RunOptions(cost_budget=0.05))
    await _run(s, 0, "use:1:1:0:0.03")
    await _run(s, 1, "use:1:1:0:0.03")
    assert s.budget.value("cost_usd") == pytest.approx(0.06, abs=1e-12)
    with pytest.raises(BudgetExhaustedError) as info:
        await _run(s, 2, "use:1:1:0:0.03")
    assert str(info.value) == "cost budget exhausted: $0.0600 of $0.0500 spent"


async def test_budgetctr_output_message_unchanged() -> None:
    s = Scheduler(UsageDispatcher(), RunOptions(budget_total=25))
    for n in range(3):
        await _run(s, n, "use:1:10:0:0")
    with pytest.raises(BudgetExhaustedError) as info:
        await _run(s, 3, "use:1:10:0:0")
    assert str(info.value) == "budget exhausted: 30 of 25 output tokens spent"


def test_budgetctr_options_validation() -> None:
    for bad in (-1, 1.5, True, "1"):
        with pytest.raises(ValueError, match="input_budget"):
            RunOptions(input_budget=bad)  # type: ignore[arg-type]
    for bad_cost in (-0.1, float("nan"), float("inf"), True, "1"):
        with pytest.raises(ValueError, match="cost_budget"):
            RunOptions(cost_budget=bad_cost)  # type: ignore[arg-type]
    for bad_wall in (0, -1, float("inf"), float("nan"), True, "1"):
        with pytest.raises(ValueError, match="wall_budget"):
            RunOptions(wall_budget=bad_wall)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="count_cache"):
        RunOptions(count_cache=1)  # type: ignore[arg-type]
    ok = RunOptions(input_budget=0, cost_budget=0, wall_budget=0.5, count_cache=False)
    assert (ok.input_budget, ok.cost_budget, ok.wall_budget, ok.count_cache) == (0, 0, 0.5, False)
