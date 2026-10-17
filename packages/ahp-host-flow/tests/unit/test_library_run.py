"""The library API: `async with ahp_host_flow.run(...)` runs in-process."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import ahp_host_flow
from ahp_host_flow import library
from ahp_host_flow.engine import BudgetExhaustedError, RunOptions
from ahp_host_flow.engine.scheduler import AgentLimitError

from ..fakes import FakeDispatcher  # noqa: TID252


async def test_library_run_agent_parallel() -> None:
    dispatcher = FakeDispatcher()
    async with library.run(dispatcher) as r:
        a = await r.agent("a", label="L")
        xs = await r.parallel([lambda: r.agent("b"), lambda: r.agent("c")])
    assert (a, xs) == ("a", ["b", "c"])
    assert [(c.ordinal, c.label) for c in dispatcher.calls] == [(0, "L"), (1, None), (2, None)]


async def test_library_run_pipeline_and_phase() -> None:
    dispatcher = FakeDispatcher()
    phases: list[str] = []
    async with library.run(dispatcher, on_phase=phases.append) as r:
        r.phase("P")
        out = await r.pipeline(["x", "y"], lambda prev, item, index: r.agent(prev))
    assert out == ["x", "y"]
    assert {c.phase for c in dispatcher.calls} == {"P"}
    assert phases == ["P"]


async def test_library_run_budget() -> None:
    dispatcher = FakeDispatcher(output_tokens=10)
    async with library.run(dispatcher, RunOptions(budget_total=15)) as r:
        await r.agent("a")
        await r.agent("b")
        with pytest.raises(BudgetExhaustedError) as info:
            await r.agent("c")
        assert str(info.value) == "budget exhausted: 20 of 15 output tokens spent"
        assert r.budget.remaining() == 0
        assert r.budget.total == 15
        assert r.budget.spent() == 20


async def test_library_run_cap() -> None:
    async with library.run(FakeDispatcher(), RunOptions(max_agents=1)) as r:
        await r.agent("a")
        with pytest.raises(AgentLimitError) as info:
            await r.agent("b")
    assert str(info.value) == "agent() limit reached: at most 1 agent() calls per run"


async def test_library_run_journal_resume(tmp_path: Path) -> None:
    first = FakeDispatcher()
    options = RunOptions(state_dir=tmp_path, run_id="first")
    async with library.run(first, options) as r:
        assert r.run_id == "first"
        values = [await r.agent("a"), await r.agent("b")]
    journal = tmp_path / "runs" / "first" / "journal.jsonl"
    header: dict[str, Any] = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert header["filename"] == "<library>"
    assert header["source"] is None
    second = FakeDispatcher()
    options = RunOptions(state_dir=tmp_path, run_id="second", resume_from="first")
    async with library.run(second, options) as r:
        again = [await r.agent("a"), await r.agent("b")]
    assert again == values
    assert second.calls == []


async def test_library_run_workflow_py_child(tmp_path: Path) -> None:
    wf = tmp_path / "wf"
    wf.mkdir()
    (wf / "child.py").write_text(
        "async def main(args):\n    return [args['who'], await agent('child')]\n",
        encoding="utf-8",
    )
    dispatcher = FakeDispatcher()
    async with library.run(dispatcher, RunOptions(workflow_dirs=(wf,))) as r:
        await r.agent("first")
        result = await asyncio.wait_for(r.workflow("child", {"who": "x"}), 60)
    assert result == ["x", "child"]
    assert [(c.ordinal, c.prompt) for c in dispatcher.calls] == [(0, "first"), (1, "child")]


async def test_library_run_no_runner_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a library run must not start a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    logs: list[str] = []
    async with library.run(FakeDispatcher(), on_log=logs.append) as r:
        r.phase("P")
        r.log("hello")
        assert await r.agent("a") == "a"
        assert await r.parallel([lambda: r.agent("b")]) == ["b"]
        assert await r.pipeline(["c"], lambda prev, item, index: r.agent(prev)) == ["c"]
    assert "hello" in logs


def test_library_run_exported() -> None:
    assert ahp_host_flow.run is library.run
    assert ahp_host_flow.FlowRun is library.FlowRun
