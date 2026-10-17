"""The engine Scheduler: cap, limits, abort and null-on-failure, on a fake dispatcher."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ahp_host_flow.engine import (
    AbortSignal,
    AgentCall,
    AgentLimitError,
    AgentResult,
    BudgetExhaustedError,
    Dispatcher,
    RunOptions,
    Scheduler,
    default_concurrency,
)

from ..fakes import FakeDispatcher  # noqa: TID252


def _call(n: int, prompt: str) -> AgentCall:
    return AgentCall(ordinal=n, prompt=prompt)


def _make(
    dispatcher: Dispatcher, options: RunOptions | None = None, **kwargs: Any
) -> tuple[Scheduler, list[str]]:
    logs: list[str] = []
    return Scheduler(dispatcher, options, on_log=logs.append, **kwargs), logs


async def _wait(coro: Any) -> Any:
    return await asyncio.wait_for(coro, 10)


class _NanDispatcher:
    async def run_agent(self, call: AgentCall) -> AgentResult:
        return AgentResult(value=float("nan"))


async def test_scheduler_returns_value() -> None:
    s, logs = _make(FakeDispatcher())
    assert await _wait(s.run(_call(0, "a"))) == "a"
    assert logs == []


async def test_scheduler_null_on_error_logs() -> None:
    s, logs = _make(FakeDispatcher())
    assert await _wait(s.run(_call(0, "fail"))) is None
    assert logs == ["agent() #0 failed: fake failure"]


async def test_scheduler_null_on_skip() -> None:
    s, logs = _make(FakeDispatcher())
    assert await _wait(s.run(_call(0, "skip"))) is None
    assert logs == []


async def test_scheduler_dispatcher_cancel_is_skip() -> None:
    s, logs = _make(FakeDispatcher())
    assert await _wait(s.run(_call(0, "cancel"))) is None
    assert logs == ["agent() #0 skipped"]


async def test_scheduler_non_json_value_is_null() -> None:
    s, logs = _make(_NanDispatcher())
    assert await _wait(s.run(_call(0, "a"))) is None
    assert len(logs) == 1
    assert "failed: result is not JSON" in logs[0]


async def test_scheduler_concurrency_cap() -> None:
    d = FakeDispatcher(hold=0.05)
    s, _ = _make(d, RunOptions(concurrency=3))
    # All 12 start in one loop iteration: 12 in flight without the cap, exactly 3 with it.
    await _wait(asyncio.gather(*(s.run(_call(i, f"p{i}")) for i in range(12))))
    assert d.max_in_flight == 3
    assert len(d.calls) == 12


def test_scheduler_default_concurrency() -> None:
    assert Scheduler(FakeDispatcher()).concurrency == default_concurrency()


def test_scheduler_agent_limit() -> None:
    s = Scheduler(FakeDispatcher(), RunOptions(max_agents=2))
    s.admit(_call(0, "a"))
    s.admit(_call(1, "b"))
    with pytest.raises(AgentLimitError) as info:
        s.admit(_call(2, "c"))
    assert str(info.value) == "agent() limit reached: at most 2 agent() calls per run"


async def test_scheduler_budget() -> None:
    s = Scheduler(FakeDispatcher(output_tokens=10), RunOptions(budget_total=15))
    s.admit(_call(0, "p0"))
    await _wait(s.run(_call(0, "p0")))
    assert s.budget.spent == 10
    s.admit(_call(1, "p1"))
    await _wait(s.run(_call(1, "p1")))
    assert s.budget.spent == 20
    with pytest.raises(BudgetExhaustedError) as info:
        s.admit(_call(2, "p2"))
    assert str(info.value) == "budget exhausted: 20 of 15 output tokens spent"


async def test_scheduler_budget_rechecked_after_slot() -> None:
    d = FakeDispatcher(hold=0.05, output_tokens=10)
    s = Scheduler(d, RunOptions(concurrency=1, budget_total=10))
    s.admit(_call(0, "p0"))
    s.admit(_call(1, "p1"))
    results = await _wait(
        asyncio.gather(s.run(_call(0, "p0")), s.run(_call(1, "p1")), return_exceptions=True)
    )
    assert results[0] == "p0"
    assert isinstance(results[1], BudgetExhaustedError)
    assert len(d.calls) == 1


async def test_scheduler_abort_cancels_inflight() -> None:
    d = FakeDispatcher()
    s, logs = _make(d)
    task = asyncio.create_task(s.run(_call(0, "hang")))
    await _wait(d.wait_started("hang"))
    s.abort()
    assert await _wait(task) is None
    assert d.cancelled == [0]
    assert logs == ["agent() #0 aborted"]
    assert await _wait(s.run(_call(1, "x"))) is None
    assert len(d.calls) == 1


async def test_scheduler_abort_signal() -> None:
    d = FakeDispatcher()
    signal = AbortSignal()
    s, logs = _make(d, abort=signal)
    task = asyncio.create_task(s.run(_call(0, "hang")))
    await _wait(d.wait_started("hang"))
    signal.abort()
    assert await _wait(task) is None
    assert d.cancelled == [0]
    assert logs == ["agent() #0 aborted"]
    assert await _wait(s.run(_call(1, "x"))) is None
    assert len(d.calls) == 1
    s.close()


async def test_scheduler_outer_cancel_cancels_dispatch() -> None:
    d = FakeDispatcher()
    s, _ = _make(d)
    task = asyncio.create_task(s.run(_call(0, "hang")))
    await _wait(d.wait_started("hang"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await _wait(task)
    assert d.cancelled == [0]


async def test_scheduler_listeners() -> None:
    events: list[tuple[Any, ...]] = []
    s, _ = _make(
        FakeDispatcher(),
        on_agent_start=lambda c: events.append(("start", c.ordinal)),
        on_agent_end=lambda c, v: events.append(("end", c.ordinal, v)),
    )
    await _wait(s.run(_call(0, "a")))
    assert events == [("start", 0), ("end", 0, "a")]
    events.clear()
    await _wait(s.run(_call(1, "fail")))
    assert events == [("start", 1), ("end", 1, None)]
    events.clear()
    s.abort()
    await _wait(s.run(_call(2, "b")))
    assert events == []


def test_scheduler_fake_is_dispatcher() -> None:
    assert isinstance(FakeDispatcher(), Dispatcher)
