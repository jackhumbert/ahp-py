"""Reading a usage report properly: turnTotals, cache reads, cost, request scope (M16)."""

from __future__ import annotations

from typing import Any

import pytest
from ahp_protocol.ext.usage import request_usage_meta

from ahp_host_flow.bridge import TurnTee
from ahp_host_flow.engine.usage import read_usage


def test_budgetctr_usage_turn_totals() -> None:
    meta = {
        "turnTotals": {
            "inputTokens": 10,
            "outputTokens": 7,
            "cacheReadTokens": 300,
            "cacheCreationTokens": 5,
        },
        "costUsd": 0.01,
    }
    r = read_usage(input_tokens=315, output_tokens=7, cache_read_tokens=None, meta=meta)
    assert (r.input_tokens, r.cache_read_tokens, r.output_tokens) == (15, 300, 7)
    assert r.cost_usd == pytest.approx(0.01, abs=1e-12)
    assert not r.request_scoped


def test_budgetctr_usage_acp() -> None:
    meta = {"acpUsage": {"cost": {"amount": 0.2, "currency": "USD"}}}
    r = read_usage(input_tokens=9, output_tokens=3, cache_read_tokens=4, meta=meta)
    assert (r.input_tokens, r.cache_read_tokens, r.output_tokens) == (9, 4, 3)
    assert r.cost_usd == pytest.approx(0.2, abs=1e-12)
    eur = {"acpUsage": {"cost": {"amount": 0.2, "currency": "EUR"}}}
    r = read_usage(input_tokens=1, output_tokens=1, cache_read_tokens=0, meta=eur)
    assert r.cost_usd is None
    bad = read_usage(input_tokens=-1, output_tokens=True, cache_read_tokens=None, meta=None)
    assert (bad.input_tokens, bad.output_tokens, bad.cache_read_tokens, bad.cost_usd) == (
        0,
        0,
        0,
        None,
    )


def test_budgetctr_usage_request_scope() -> None:
    r = read_usage(input_tokens=5, output_tokens=2, cache_read_tokens=1, meta=request_usage_meta())
    assert r.request_scoped
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens) == (5, 2, 1)
    plain = read_usage(input_tokens=1, output_tokens=1, cache_read_tokens=0, meta={})
    assert not plain.request_scoped


class _Sink:
    def __init__(self) -> None:
        self.reports: list[dict[str, Any]] = []

    async def usage(self, **kwargs: Any) -> None:
        self.reports.append(kwargs)


def _totals(inp: int, out: int, cache: int, cost: float) -> dict[str, Any]:
    totals = {"inputTokens": inp, "outputTokens": out, "cacheReadTokens": cache}
    return {"turnTotals": totals, "costUsd": cost}


async def test_budgetctr_usage_tee() -> None:
    sink = _Sink()
    tee = TurnTee(sink)  # type: ignore[arg-type]
    first, second = _totals(10, 4, 100, 0.01), _totals(20, 6, 50, 0.02)
    await tee.usage(input_tokens=110, output_tokens=4, meta=first)
    await tee.usage(input_tokens=70, output_tokens=6, meta=second)
    await tee.usage(input_tokens=1, output_tokens=1, meta=request_usage_meta())
    assert (tee.input_tokens, tee.output_tokens, tee.cache_read_tokens) == (30, 10, 150)
    assert tee.cost_usd == pytest.approx(0.03, abs=1e-12)
    assert [r["meta"] for r in sink.reports[:2]] == [first, second]
    assert sink.reports[0]["input_tokens"] == 110
    assert len(sink.reports) == 3

    acp = _Sink()
    tee2 = TurnTee(acp)  # type: ignore[arg-type]
    for amount in (0.1, 0.25):
        acp_meta = {"acpUsage": {"cost": {"amount": amount, "currency": "USD"}}}
        await tee2.usage(input_tokens=2, output_tokens=1, cache_read_tokens=3, meta=acp_meta)
    assert tee2.cost_usd == pytest.approx(0.25, abs=1e-12)
    assert (tee2.input_tokens, tee2.cache_read_tokens) == (4, 6)


def test_budgetctr_usage_acp_gauge_counts_no_input() -> None:
    # ahp-host-acp's context size standing in for a missing inputTokens is no consumption.
    meta = {"acpUsage": {"used": 5000, "inputGauge": True}}
    r = read_usage(input_tokens=5000, output_tokens=3, cache_read_tokens=None, meta=meta)
    assert (r.input_tokens, r.output_tokens) == (0, 3)
    real = read_usage(input_tokens=5000, output_tokens=3, cache_read_tokens=None, meta={})
    assert real.input_tokens == 5000


async def test_budgetctr_usage_acp_cost_delta_on_a_reused_session() -> None:
    # ACP's cost is the session's running total: a later call on the same session (a named
    # chat) counts only what it added since it began.
    acp_meta = {"acpUsage": {"cost": {"amount": 0.75, "currency": "USD"}}}
    tee = TurnTee(_Sink(), cost_base=0.5)  # type: ignore[arg-type]
    await tee.usage(input_tokens=1, output_tokens=1, meta=acp_meta)
    assert tee.cost_usd == pytest.approx(0.25, abs=1e-12)
    assert tee.session_cost == pytest.approx(0.75, abs=1e-12)
