"""Every attempt of a call is counted: retries and fallbacks keep a usage slot each (M16).

A dispatcher opens one slot per attempt (`begin_usage_attempt`); the Scheduler sums the slots,
so the budget and the quota count an attempt that failed (an exhausted `retry_empty`, a primary
that failed before its fallback) as well as the one that answered.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ahp_host_flow.engine import (
    AgentCall,
    AgentResult,
    DispatchError,
    QuotaLedger,
    QuotaLimits,
    RunOptions,
    Scheduler,
    begin_usage_attempt,
    end_usage_attempt,
)


class _Attempts:
    """Runs one call as attempts: each reports its usage into its own slot, then settles.

    An attempt is (usage, outcome): outcome None fails it (the next attempt follows), a string
    is the answer, and after the last failed attempt the call raises *final*.
    """

    def __init__(
        self, attempts: Sequence[tuple[Mapping[str, Any], str | None]], final: DispatchError
    ) -> None:
        self.attempts = attempts
        self.final = final

    async def run_agent(self, call: AgentCall) -> AgentResult:
        for usage, outcome in self.attempts:
            report, token = begin_usage_attempt()
            try:
                report(usage)
                if outcome is not None:
                    return AgentResult(outcome, usage=dict(usage))
            finally:
                end_usage_attempt(token)
        raise self.final


async def _run(dispatcher: _Attempts) -> tuple[Scheduler, QuotaLedger, Any]:
    ledger = QuotaLedger(None, QuotaLimits(tokens_per_run=10_000))
    scheduler = Scheduler(
        dispatcher, RunOptions(budget_total=1000, quota=ledger.for_run("someone"))
    )
    call, cached = scheduler.issue("x", {})
    value = await scheduler.run(call, cached)
    scheduler.close()
    return scheduler, ledger, value


async def test_attempt_usage_retry_exhausted_counts_every_attempt() -> None:
    empty = DispatchError("empty", True, "empty reply 2 times")
    attempt = {"output_tokens": 30, "input_tokens": 4}
    scheduler, ledger, value = await _run(_Attempts([(attempt, None), (attempt, None)], empty))
    assert value is None
    assert scheduler.budget.spent == 60
    assert scheduler.budget.value("input_tokens") == 8
    assert ledger.day_used("someone") == (68, 0.0)


async def test_attempt_usage_failed_primary_counts_before_fallback() -> None:
    never = DispatchError("failed", False, "unused")
    primary = {"output_tokens": 40, "cost_usd": 0.5}
    backup = {"output_tokens": 10}
    scheduler, ledger, value = await _run(_Attempts([(primary, None), (backup, "ok")], never))
    assert value == "ok"
    # The result names only the fallback's usage; the primary's failed attempt is added.
    assert scheduler.budget.spent == 50
    assert scheduler.budget.value("cost_usd") == 0.5
    assert ledger.day_used("someone") == (50, 0.5)
