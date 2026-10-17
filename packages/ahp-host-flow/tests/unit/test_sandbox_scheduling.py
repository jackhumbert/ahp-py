"""`agent()` through the Scheduler: caps, budget mirror, phase/label, abort, listeners."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ahp_host_flow.engine import AbortSignal, AgentCall, RunOptions
from ahp_host_flow.sandbox import Sandbox, SandboxError, run_script

from ..fakes import FakeDispatcher  # noqa: TID252

pytestmark = pytest.mark.node


async def _run(source: str, node: str, dispatcher: FakeDispatcher, **kwargs: Any) -> Any:
    return await asyncio.wait_for(
        run_script(source, node=node, dispatcher=dispatcher, **kwargs), 30
    )


async def test_sandbox_scheduling_concurrency_cap(node_path: str) -> None:
    d = FakeDispatcher(hold=0.05)
    source = "return await Promise.all(Array.from({length: 12}, (_, i) => agent('c' + i)))"
    result = await _run(source, node_path, d, options=RunOptions(concurrency=3))
    assert result == [f"c{i}" for i in range(12)]
    assert d.max_in_flight == 3


async def test_sandbox_scheduling_agent_limit(node_path: str) -> None:
    d = FakeDispatcher()
    source = """
const r = [await agent('a'), await agent('b')];
try { await agent('c') } catch (e) { return [r, e.message] }
"""
    result = await _run(source, node_path, d, options=RunOptions(max_agents=2))
    assert result == [["a", "b"], "agent() limit reached: at most 2 agent() calls per run"]
    assert len(d.calls) == 2


async def test_sandbox_scheduling_budget_mirror(node_path: str) -> None:
    d = FakeDispatcher(output_tokens=10)
    source = """
const s = [budget.total, budget.spent(), budget.remaining()];
await agent('a');
s.push(budget.spent(), budget.remaining());
await agent('b');
s.push(budget.spent(), budget.remaining());
try { await agent('c') } catch (e) { s.push(e.message) }
return s
"""
    result = await _run(source, node_path, d, options=RunOptions(budget_total=15))
    assert result == [15, 0, 15, 10, 5, 20, 0, "budget exhausted: 20 of 15 output tokens spent"]
    assert len(d.calls) == 2


async def test_sandbox_scheduling_budget_unset(node_path: str) -> None:
    d = FakeDispatcher(output_tokens=10)
    source = (
        "await agent('a'); return [budget.total, budget.spent(), budget.remaining() === Infinity]"
    )
    assert await _run(source, node_path, d) == [None, 10, True]


async def test_sandbox_scheduling_budget_zero(node_path: str) -> None:
    d = FakeDispatcher()
    source = "try { await agent('a') } catch (e) { return e.message }"
    result = await _run(source, node_path, d, options=RunOptions(budget_total=0))
    assert result == "budget exhausted: 0 of 0 output tokens spent"


async def test_sandbox_scheduling_budget_frozen(node_path: str) -> None:
    source = (
        "try { budget.total = 1; return 'mutated' } catch (e) { return e instanceof TypeError }"
    )
    assert await _run(source, node_path, FakeDispatcher()) is True


async def test_sandbox_scheduling_phase_and_label(node_path: str) -> None:
    d = FakeDispatcher()
    source = """
await agent('z');
phase('Map');
await agent('a', {label: 'L'});
await agent('b', {phase: 'P'});
await agent('c', {phase: 3});
return 0
"""
    await _run(source, node_path, d)
    assert [(c.ordinal, c.phase, c.label) for c in d.calls] == [
        (0, None, None),
        (1, "Map", "L"),
        (2, "P", None),
        (3, "Map", None),
    ]
    assert dict(d.calls[1].opts) == {"label": "L"}


async def test_sandbox_scheduling_listeners(node_path: str) -> None:
    d = FakeDispatcher()
    starts: list[int] = []
    ends: list[tuple[int, Any]] = []

    def on_end(call: AgentCall, value: Any) -> None:
        ends.append((call.ordinal, value))

    await _run(
        "await agent('a'); await agent('fail'); return 0",
        node_path,
        d,
        on_agent_start=lambda call: starts.append(call.ordinal),
        on_agent_end=on_end,
    )
    assert starts == [0, 1]
    assert ends == [(0, "a"), (1, None)]


async def test_sandbox_scheduling_null_on_skip(node_path: str) -> None:
    d = FakeDispatcher()
    logs: list[str] = []
    result = await _run(
        "return [await agent('skip'), await agent('cancel')]", node_path, d, on_log=logs.append
    )
    assert result == [None, None]
    assert any("agent() #1 skipped" in line for line in logs)


async def test_sandbox_scheduling_abort(node_path: str) -> None:
    d = FakeDispatcher()
    signal = AbortSignal()
    source = 'const v = await agent("hang"); const w = await agent("after"); return [v, w]'
    task = asyncio.create_task(_run(source, node_path, d, abort=signal))
    await asyncio.wait_for(d.wait_started("hang"), 30)
    signal.abort()
    assert await task == [None, None]
    assert d.cancelled == [0]
    assert [c.prompt for c in d.calls] == ["hang"]


async def test_sandbox_scheduling_cancel_run(node_path: str) -> None:
    d = FakeDispatcher()
    async with Sandbox(node_path) as sb:
        task = asyncio.create_task(sb.run('return await agent("hang")', dispatcher=d))
        await asyncio.wait_for(d.wait_started("hang"), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert d.cancelled == [0]
        with pytest.raises(SandboxError):
            await asyncio.wait_for(sb.run("return 1", dispatcher=d), 30)


async def test_raising_listener_fails_the_call_not_the_run(node_path: str) -> None:
    # A host-side bug answers the script with an error instead of leaving it waiting.
    def boom(_call: object) -> None:
        raise RuntimeError("listener broke")

    source = "try { await agent('x') } catch (e) { return e.message }"
    message = await asyncio.wait_for(
        run_script(source, node=node_path, dispatcher=FakeDispatcher(), on_agent_start=boom), 10
    )
    assert "listener broke" in message
