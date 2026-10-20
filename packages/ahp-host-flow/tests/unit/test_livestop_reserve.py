"""Admission reservations under budget_total: parallel calls cannot overshoot it (M16)."""

from __future__ import annotations

import asyncio
from typing import Any

from ahp_host_flow import library
from ahp_host_flow.engine import BudgetExhaustedError, RunOptions

from ..limits_fakes import UsageDispatcher  # noqa: TID252


async def _burst(
    dispatcher: UsageDispatcher, options: RunOptions, prompt: str
) -> tuple[list[Any], int]:
    """Eight calls issued together: each result (the value or the exception) and the spend."""
    async with library.run(dispatcher, options) as flow:
        futures = [flow.agent(prompt) for _ in range(8)]
        results = await asyncio.wait_for(asyncio.gather(*futures, return_exceptions=True), 10)
        return results, flow.budget.spent()


async def test_livestop_reserve_eight_parallel() -> None:
    dispatcher = UsageDispatcher()
    options = RunOptions(budget_total=200, reserve=100, concurrency=8)
    results, spent = await _burst(dispatcher, options, "slow:0.05:100")
    assert len(dispatcher.calls) == 2
    assert spent == 200 <= 200 + 100
    refused = [r for r in results if isinstance(r, BudgetExhaustedError)]
    assert len(refused) == 6
    assert results[:2] == ["slow:0.05:100", "slow:0.05:100"]
    assert len(dispatcher.cancelled) == 0


async def test_livestop_reserve_zero_control() -> None:
    dispatcher = UsageDispatcher()
    options = RunOptions(budget_total=200, reserve=0, concurrency=8)
    _, spent = await _burst(dispatcher, options, "slow:0.05:100")
    assert len(dispatcher.calls) == 8
    assert spent == 800


async def test_livestop_reserve_mean() -> None:
    dispatcher = UsageDispatcher()
    options = RunOptions(budget_total=300, reserve=None, concurrency=8)
    async with library.run(dispatcher, options) as flow:
        assert await flow.agent("slow:0:100") == "slow:0:100"
        futures = [flow.agent("slow:0.05:100") for _ in range(8)]
        results = await asyncio.wait_for(asyncio.gather(*futures, return_exceptions=True), 10)
    assert len(dispatcher.calls) == 3
    assert sum(isinstance(r, BudgetExhaustedError) for r in results) == 6
