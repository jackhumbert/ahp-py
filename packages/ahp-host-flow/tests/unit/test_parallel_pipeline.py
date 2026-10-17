"""`parallel()` and `pipeline()` in the sandbox prelude."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ahp_host_flow.engine import RunOptions
from ahp_host_flow.sandbox import run_script

from ..fakes import FakeDispatcher  # noqa: TID252

pytestmark = pytest.mark.node


async def _run(
    source: str, node: str, dispatcher: FakeDispatcher, timeout: float = 30, **kwargs: Any
) -> Any:
    return await asyncio.wait_for(
        run_script(source, node=node, dispatcher=dispatcher, **kwargs), timeout
    )


async def test_parallel_pipeline_parallel_nulls(node_path: str) -> None:
    source = """
return await parallel([
  () => agent('a'),
  () => { throw new Error('x') },
  async () => { throw new Error('y') },
  () => agent('fail'),
  () => 42,
  'not a function',
])
"""
    assert await _run(source, node_path, FakeDispatcher()) == ["a", None, None, None, 42, None]


async def test_parallel_pipeline_parallel_barrier(node_path: str) -> None:
    source = """
const order = [];
const r = await parallel([
  async () => { const v = await agent('slow'); order.push('slow'); return v },
  async () => { order.push('fast'); return 1 },
]);
order.push('after');
return [r, order]
"""
    result = await _run(source, node_path, FakeDispatcher(hold=0.05))
    assert result == [["slow", 1], ["fast", "slow", "after"]]


async def test_parallel_pipeline_parallel_cap(node_path: str) -> None:
    source = """
try { await parallel(new Array(4097).fill(() => 1)) }
catch (e) {
  const ok = await parallel(new Array(4096).fill(() => 1));
  return [e instanceof RangeError, e.message, ok.length]
}
"""
    result = await _run(source, node_path, FakeDispatcher())
    assert result == [True, "parallel() takes at most 4096 items, got 4097", 4096]


async def test_parallel_pipeline_parallel_rejects_non_array(node_path: str) -> None:
    source = "try { await parallel(1) } catch (e) { return e.message }"
    result = await _run(source, node_path, FakeDispatcher())
    assert result == "parallel() needs an array of functions"


async def test_parallel_pipeline_pipeline_stages(node_path: str) -> None:
    source = """
const seen = [];
const out = await pipeline(['x', 'y', 'z'],
  async (prev, item, index) => {
    seen.push(['s1', prev, item, index]);
    if (item === 'y') throw new Error('drop');
    return agent(prev + '1')
  },
  async (prev, item, index) => { seen.push(['s2', prev, item, index]); return prev + '2' });
const s2 = seen.filter(s => s[0] === 's2').sort((a, b) => a[3] - b[3]);
return [out, seen.filter(s => s[0] === 's1'), s2]
"""
    result = await _run(source, node_path, FakeDispatcher())
    assert result == [
        ["x12", None, "z12"],
        [["s1", "x", "x", 0], ["s1", "y", "y", 1], ["s1", "z", "z", 2]],
        [["s2", "x1", "x", 0], ["s2", "z1", "z", 2]],
    ]


async def test_parallel_pipeline_pipeline_no_barrier(node_path: str) -> None:
    # Item b's first stage finishes only after item a's second stage has started;
    # a barrier between stages would deadlock and the timeout would fire.
    source = """
return await pipeline(['a', 'b'],
  (p, item) => item === 'a' ? agent('a1') : agent('wait-for:a2'),
  (prev, item) => item === 'a' ? agent('a2') : prev)
"""
    result = await _run(
        source, node_path, FakeDispatcher(), timeout=10, options=RunOptions(concurrency=4)
    )
    assert result == ["a2", "wait-for:a2"]


async def test_parallel_pipeline_pipeline_cap_and_passthrough(node_path: str) -> None:
    source = """
const out = [];
try { await pipeline(new Array(4097).fill(1), (p) => p) } catch (e) { out.push(e.message) }
out.push(await pipeline([1, 2]));
try { await pipeline([1], 'nope') } catch (e) { out.push(e.message) }
return out
"""
    result = await _run(source, node_path, FakeDispatcher())
    assert result == [
        "pipeline() takes at most 4096 items, got 4097",
        [1, 2],
        "pipeline() stages must be functions",
    ]
