"""The live stop: usage reports and wall time cancel in-flight calls; resume reruns them (M16)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ahp_host_flow import library
from ahp_host_flow.engine import BudgetExhaustedError, EchoDispatcher, RunOptions, load_journal

from ..limits_fakes import UsageDispatcher  # noqa: TID252

PROMPTS = ["hang one", "hang two", "burn:150"]


async def _stopped_run(
    tmp_path: Path, logs: list[str]
) -> tuple[UsageDispatcher, str, list[object]]:
    dispatcher = UsageDispatcher()
    options = RunOptions(budget_total=100, state_dir=tmp_path / "state", concurrency=4)
    async with library.run(dispatcher, options, on_log=logs.append) as flow:
        futures = [flow.agent(p) for p in PROMPTS]
        results = await asyncio.wait_for(asyncio.gather(*futures, return_exceptions=True), 10)
        assert flow.run_id is not None
        return dispatcher, flow.run_id, list(results)


async def test_livestop_cancels_in_flight(tmp_path: Path) -> None:
    logs: list[str] = []
    dispatcher, run_id, results = await _stopped_run(tmp_path, logs)
    assert sorted(dispatcher.cancelled) == [0, 1, 2]
    assert all(isinstance(r, BudgetExhaustedError) for r in results)
    agents = load_journal(tmp_path / "state", run_id).agents
    assert [(a.status, a.kind) for a in agents] == [("error", "budget")] * 3
    assert sorted(a.output_tokens for a in agents) == [0, 0, 150]
    stops = [line for line in logs if line.endswith("; stopping 3 agent() call(s) in flight")]
    assert len(stops) == 1
    assert stops[0].startswith("budget exhausted: 150 of 100 output tokens spent")


async def test_livestop_resume_reruns(tmp_path: Path) -> None:
    _, run_id, _ = await _stopped_run(tmp_path, [])
    dispatcher = EchoDispatcher()
    options = RunOptions(state_dir=tmp_path / "state", resume_from=run_id)
    async with library.run(dispatcher, options) as flow:
        results = await asyncio.wait_for(asyncio.gather(*[flow.agent(p) for p in PROMPTS]), 10)
    # Served live, not from the cache: a stopped call is never replayed as null.
    assert results == PROMPTS
    assert [c.prompt for c in dispatcher.calls] == PROMPTS


async def test_livestop_wall_budget() -> None:
    dispatcher = UsageDispatcher()
    logs: list[str] = []
    t0 = time.monotonic()
    async with library.run(dispatcher, RunOptions(wall_budget=0.2), on_log=logs.append) as flow:
        result = await asyncio.wait_for(
            asyncio.gather(flow.agent("hang"), return_exceptions=True), 10
        )
    assert time.monotonic() - t0 < 2.0
    (error,) = result
    assert isinstance(error, BudgetExhaustedError)
    assert str(error).startswith("wall budget exhausted")
    assert dispatcher.cancelled == [0]
    assert any("; stopping 1 agent() call(s) in flight" in line for line in logs)
