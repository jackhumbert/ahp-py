"""ACP's `_meta.turnTokenTotals` (the turn sum) is what an ACP turn charges, not `inputTokens`."""

from __future__ import annotations

from typing import Any

import pytest

from ahp_host_flow.bridge import TurnTee
from ahp_host_flow.engine import Budget
from ahp_host_flow.engine.usage import read_usage


def _acp_rounds_report() -> dict[str, Any]:
    # Exactly what ahp-host-acp's `_report_usage` sends for a three-round turn whose
    # usage_updates said used 100, 200, 300 and whose ACP turn usage summed the rounds
    # (its `test_usage_reports_the_latest_call_and_the_turn_sum_apart`).
    return {
        "input_tokens": 300,
        "output_tokens": 40,
        "cache_read_tokens": 7,
        "model": "a",
        "meta": {
            "acpUsage": {"used": 300, "size": 200000, "cost": {"amount": 0.5, "currency": "USD"}},
            "turnTokenTotals": [
                {"model": "a", "inputTokens": 600, "cachedTokens": 7, "outputTokens": 40}
            ],
        },
    }


def test_acp_turn_totals_read() -> None:
    report = _acp_rounds_report()
    del report["model"]
    r = read_usage(**report)
    assert (r.input_tokens, r.cache_read_tokens, r.output_tokens) == (600, 7, 40)
    assert r.cost_usd == pytest.approx(0.5, abs=1e-12)


def test_acp_turn_totals_every_entry_counts() -> None:
    # One entry per model; a bad entry or value counts 0.
    meta = {
        "turnTokenTotals": [
            {"model": "a", "inputTokens": 10, "cachedTokens": 1, "outputTokens": 2},
            {"model": "b", "inputTokens": 5, "cachedTokens": True, "outputTokens": -3},
            "junk",
        ]
    }
    r = read_usage(input_tokens=99, output_tokens=99, cache_read_tokens=99, meta=meta)
    assert (r.input_tokens, r.cache_read_tokens, r.output_tokens) == (15, 1, 2)


class _Sink:
    async def usage(self, **kwargs: Any) -> None:
        pass


async def test_acp_turn_totals_budget_charges_the_turn_sum() -> None:
    tee = TurnTee(_Sink())  # type: ignore[arg-type]
    await tee.usage(**_acp_rounds_report())
    budget = Budget(input_total=10_000)
    usage = {
        "input_tokens": tee.input_tokens,
        "output_tokens": tee.output_tokens,
        "cache_read_tokens": tee.cache_read_tokens,
    }
    assert budget.add_result(usage, tee.cost_usd) == 40
    # 600 input plus 7 cache reads (counted by default), not the last call's 300.
    assert budget.value("input_tokens") == 607
