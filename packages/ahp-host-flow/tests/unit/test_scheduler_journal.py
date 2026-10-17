"""Scheduler journaling and cached serving, on a fake dispatcher and a temp journal."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import (
    AgentCall,
    AgentLimitError,
    AgentRecord,
    AgentResult,
    BudgetExhaustedError,
    JournalWriter,
    Replay,
    RunHeader,
    RunOptions,
    Scheduler,
    call_key,
    load_journal,
)

from ..fakes import FakeDispatcher  # noqa: TID252


class _NanDispatcher:
    async def run_agent(self, call: AgentCall) -> AgentResult:
        return AgentResult(value=float("nan"))


@pytest.fixture
def writer(tmp_path: Path) -> Iterator[JournalWriter]:
    w = JournalWriter.create(tmp_path, RunHeader("r1", "s.js", "src", False, None, 1000, None))
    yield w
    w.close()


def _call(n: int, prompt: str) -> AgentCall:
    return AgentCall(ordinal=n, prompt=prompt)


def _agents(tmp_path: Path) -> tuple[AgentRecord, ...]:
    return load_journal(tmp_path, "r1").agents


async def _wait(coro: Any) -> Any:
    return await asyncio.wait_for(coro, 10)


async def _run(s: Scheduler, call: AgentCall) -> Any:
    cached = s.admit(call)
    return await _wait(s.run(call, cached))


async def test_scheduler_journal_statuses(tmp_path: Path, writer: JournalWriter) -> None:
    s = Scheduler(FakeDispatcher(), journal=writer)
    for n, prompt in enumerate(["a", "skip", "cancel", "fail"]):
        await _run(s, _call(n, prompt))
    await _run(Scheduler(_NanDispatcher(), journal=writer), _call(4, "nan"))
    recs = _agents(tmp_path)
    assert [r.status for r in recs] == ["ok", "null", "null", "error", "error"]
    assert recs[0].value == "a"
    assert recs[0].key == call_key(_call(0, "a"))
    assert recs[3].error == "fake failure"
    assert (recs[4].error or "").startswith("result is not JSON")


async def test_scheduler_journal_output_tokens(tmp_path: Path, writer: JournalWriter) -> None:
    s = Scheduler(FakeDispatcher(output_tokens=7), journal=writer)
    await _run(s, _call(0, "a"))
    assert _agents(tmp_path)[0].output_tokens == 7


async def test_scheduler_journal_abort_writes_no_record(
    tmp_path: Path, writer: JournalWriter
) -> None:
    d = FakeDispatcher()
    s = Scheduler(d, journal=writer)
    call = _call(0, "hang")
    task = asyncio.create_task(s.run(call, s.admit(call)))
    await _wait(d.wait_started("hang"))
    s.abort()
    assert await _wait(task) is None
    assert _agents(tmp_path) == ()


def _cached(status: Any = "ok", value: Any = "v", error: str | None = None) -> AgentRecord:
    return AgentRecord(0, call_key(_call(0, "p")), status, value, 5, error)


async def test_scheduler_journal_cached_call(tmp_path: Path, writer: JournalWriter) -> None:
    d = FakeDispatcher()
    rec = _cached()
    started: list[AgentCall] = []
    s = Scheduler(d, journal=writer, replay=Replay([rec]), on_agent_start=started.append)
    call = _call(0, "p")
    assert s.admit(call) == rec
    assert await _wait(s.run(call, rec)) == "v"
    assert d.calls == []
    assert started == []
    assert s.budget.spent == 5
    assert _agents(tmp_path) == (rec,)


@pytest.mark.parametrize("rec", [_cached("null", None), _cached("error", None, "boom")])
async def test_scheduler_journal_cached_non_ok_is_null(
    tmp_path: Path, writer: JournalWriter, rec: AgentRecord
) -> None:
    s = Scheduler(FakeDispatcher(), journal=writer, replay=Replay([rec]))
    assert await _run(s, _call(0, "p")) is None
    assert _agents(tmp_path) == (rec,)


async def test_scheduler_journal_cached_skips_budget_check() -> None:
    rec = _cached()
    s = Scheduler(FakeDispatcher(), RunOptions(budget_total=0), replay=Replay([rec]))
    assert s.admit(_call(0, "p")) == rec
    with pytest.raises(BudgetExhaustedError):
        s.admit(_call(1, "q"))


async def test_scheduler_journal_cached_still_capped() -> None:
    rec = AgentRecord(1, call_key(_call(1, "p")), "ok", "v")
    s = Scheduler(FakeDispatcher(), RunOptions(max_agents=1), replay=Replay([rec]))
    with pytest.raises(AgentLimitError):
        s.admit(_call(1, "p"))


async def test_scheduler_journal_mismatch_goes_live() -> None:
    logs: list[str] = []
    rec = _cached()
    s = Scheduler(
        FakeDispatcher(),
        replay=Replay([rec, AgentRecord(1, "other", "ok", "x")], on_log=logs.append),
    )
    assert s.admit(_call(0, "p")) == rec
    assert s.admit(_call(1, "different")) is None
    assert any("cache ended at agent() #1" in line for line in logs)


async def test_scheduler_journal_aborted_cached_not_journaled(
    tmp_path: Path, writer: JournalWriter
) -> None:
    rec = _cached()
    logs: list[str] = []
    s = Scheduler(FakeDispatcher(), journal=writer, replay=Replay([rec]), on_log=logs.append)
    call = _call(0, "p")
    cached = s.admit(call)
    s.abort()
    assert await _wait(s.run(call, cached)) is None
    assert logs == ["agent() #0 aborted"]
    assert _agents(tmp_path) == ()
