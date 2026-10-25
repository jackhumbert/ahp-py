"""A call cancelled from outside still charges what it reported to the budget and the quota."""

from __future__ import annotations

import asyncio

import pytest

from ahp_host_flow.engine import (
    AgentCall,
    AgentResult,
    QuotaLedger,
    QuotaLimits,
    RunOptions,
    Scheduler,
    usage_reporter,
)


class _ReportThenHang:
    """Reports 40 output and 10 input tokens, then waits until cancelled."""

    def __init__(self) -> None:
        self.reported = asyncio.Event()

    async def run_agent(self, call: AgentCall) -> AgentResult:
        usage_reporter()({"output_tokens": 40, "input_tokens": 10})
        self.reported.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.parametrize("outer_cancel", [False, True], ids=["abort", "abort+cancel"])
async def test_scheduler_cancelled_call_charges_reported_usage(outer_cancel: bool) -> None:
    ledger = QuotaLedger(None, QuotaLimits(tokens_per_day=10_000), today=lambda: "2026-01-01")
    dispatcher = _ReportThenHang()
    scheduler = Scheduler(dispatcher, RunOptions(budget_total=1000, quota=ledger.for_run("alice")))
    call, cached = scheduler.issue("p", {})
    task = asyncio.create_task(scheduler.run(call, cached))
    await asyncio.wait_for(dispatcher.reported.wait(), 5)
    scheduler.abort()
    if outer_cancel:
        # The sandbox's run cancels its agent() tasks right after the abort.
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert ledger.day_used("alice") == (50, 0.0)
    assert scheduler.budget.spent == 40
    assert scheduler.budget.value("input_tokens") == 10
    scheduler.close()
