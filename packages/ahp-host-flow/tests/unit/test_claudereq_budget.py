"""Claude's per-request usage and its settled turn report count the same input (M16).

With ahp-host-claude's real `request_report` and `report` output: a request's input is its
uncached prompt plus cache creation, cache reads apart, exactly as the turn report's
`turnTotals` reads, so the live counter and the settled one agree for both `count_cache`
settings (no cache read is counted twice).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ahp_host_claude.usage import report, request_report

from ahp_host_flow.engine import (
    INPUT_TOKENS,
    AgentCall,
    AgentResult,
    RunOptions,
    Scheduler,
    usage_reporter,
)
from ahp_host_flow.engine.usage import read_usage

REQUEST = {
    "input_tokens": 10,
    "cache_read_input_tokens": 300,
    "cache_creation_input_tokens": 5,
    "output_tokens": 7,
}


def _reading(rep: Any) -> dict[str, int]:
    r = read_usage(
        input_tokens=rep.input_tokens,
        output_tokens=rep.output_tokens,
        cache_read_tokens=rep.cache_read_tokens,
        meta=rep.meta,
    )
    return {
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cache_read_tokens": r.cache_read_tokens,
    }


def test_claudereq_budget_request_and_turn_read_alike() -> None:
    live = _reading(request_report(REQUEST, model="m"))
    settled = _reading(report(REQUEST, REQUEST, model="m"))
    assert live == settled == {"input_tokens": 15, "output_tokens": 7, "cache_read_tokens": 300}


@pytest.mark.parametrize(("count_cache", "expected"), [(True, 315), (False, 15)])
async def test_claudereq_budget_live_matches_settled(count_cache: bool, expected: int) -> None:
    live = _reading(request_report(REQUEST, model="m"))
    settled = _reading(report(REQUEST, REQUEST, model="m"))
    seen: list[float] = []
    reported = asyncio.Event()
    proceed = asyncio.Event()

    class _Dispatcher:
        async def run_agent(self, call: AgentCall) -> AgentResult:
            usage_reporter()(live)
            reported.set()
            await proceed.wait()
            return AgentResult("ok", usage=settled)

    scheduler = Scheduler(_Dispatcher(), RunOptions(count_cache=count_cache))
    call, cached = scheduler.issue("x", {})
    task = asyncio.create_task(scheduler.run(call, cached))
    await reported.wait()
    seen.append(scheduler._effective(INPUT_TOKENS))
    proceed.set()
    assert await task == "ok"
    seen.append(scheduler.budget.value(INPUT_TOKENS))
    scheduler.close()
    assert seen == [expected, expected]
