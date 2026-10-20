"""RunOptions.reserve defaults to the mean of finished calls (M16)."""

from __future__ import annotations

import asyncio

import pytest

from ahp_host_flow import library
from ahp_host_flow.engine import BudgetExhaustedError, RunOptions

from ..limits_fakes import UsageDispatcher  # noqa: TID252


async def test_reservemean_default_is_mean() -> None:
    assert RunOptions().reserve is None
    dispatcher = UsageDispatcher()
    options = RunOptions(budget_total=300, concurrency=8)
    async with library.run(dispatcher, options) as flow:
        assert await flow.agent("slow:0:100") == "slow:0:100"
        futures = [flow.agent("slow:0.05:100") for _ in range(8)]
        await asyncio.wait_for(asyncio.gather(*futures, return_exceptions=True), 10)
    # the finished call plus exactly two of the eight parallel ones
    assert len(dispatcher.calls) == 3


async def test_reservemean_sequential_unchanged() -> None:
    dispatcher = UsageDispatcher()
    options = RunOptions(budget_total=25)
    async with library.run(dispatcher, options) as flow:
        for _ in range(3):
            await flow.agent("slow:0:10")
        assert flow.budget.spent() == 30
        with pytest.raises(BudgetExhaustedError) as info:
            await flow.agent("slow:0:10")
    assert str(info.value) == "budget exhausted: 30 of 25 output tokens spent"
    assert len(dispatcher.calls) == 3
