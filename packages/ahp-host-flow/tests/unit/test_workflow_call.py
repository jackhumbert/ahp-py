"""workflow(): an inline child run sharing the cap, counter, budget and abort."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import AbortSignal, ClientWorkflow, RunOptions
from ahp_host_flow.sandbox import run_script

from ..fakes import FakeDispatcher  # noqa: TID252

pytestmark = pytest.mark.node

NEST_MESSAGE = "workflow() can only nest one level: a child workflow cannot call workflow()"
CHILD = (
    "export const meta = { name: 'child', description: 'c' }\n"
    "return { reply: await agent('child ' + args.who), args }"
)


def _setup(tmp_path: Path, **files: str) -> tuple[Path, RunOptions]:
    wf = tmp_path / "wf"
    wf.mkdir()
    (wf / "child.js").write_text(CHILD, encoding="utf-8")
    for name, text in files.items():
        (wf / f"{name}.js").write_text(text, encoding="utf-8")
    return wf, RunOptions(workflow_dirs=(wf,), run_root=tmp_path)


async def _run(
    source: str | None,
    node: str,
    dispatcher: FakeDispatcher,
    options: RunOptions,
    **kwargs: Any,
) -> Any:
    return await asyncio.wait_for(
        run_script(source, node=node, dispatcher=dispatcher, options=options, **kwargs), 30
    )


async def _catch(source_call: str, node: str, tmp_path: Path, **files: str) -> Any:
    _, options = _setup(tmp_path, **files)
    source = f"try {{ await {source_call} }} catch (e) {{ return e.message }}"
    return await _run(source, node, FakeDispatcher(), options)


async def test_workflow_call_by_name(node_path: str, tmp_path: Path) -> None:
    _, options = _setup(tmp_path)
    source = "return [await workflow('child', {who: 'n'}), typeof args]"
    result = await _run(source, node_path, FakeDispatcher(), options)
    assert result == [{"reply": "child n", "args": {"who": "n"}}, "undefined"]


async def test_workflow_call_by_script_path(node_path: str, tmp_path: Path) -> None:
    _, options = _setup(tmp_path)
    source = "return await workflow({scriptPath: 'wf/child.js'}, {who: 'p'})"
    result = await _run(source, node_path, FakeDispatcher(), options)
    assert result == {"reply": "child p", "args": {"who": "p"}}


async def test_workflow_call_shares_ordinals(node_path: str, tmp_path: Path) -> None:
    _, options = _setup(tmp_path)
    d = FakeDispatcher()
    source = "await agent('a'); await workflow('child', {who: 'x'}); await agent('b'); return 0"
    await _run(source, node_path, d, options)
    assert [(c.ordinal, c.prompt) for c in d.calls] == [(0, "a"), (1, "child x"), (2, "b")]


async def test_workflow_call_shares_budget(node_path: str, tmp_path: Path) -> None:
    _, base = _setup(tmp_path)
    options = RunOptions(workflow_dirs=base.workflow_dirs, run_root=tmp_path, budget_total=100)
    source = "await workflow('child', {who: 'x'}); return budget.spent()"
    assert await _run(source, node_path, FakeDispatcher(output_tokens=10), options) == 10


async def test_workflow_call_shares_agent_cap(node_path: str, tmp_path: Path) -> None:
    _, base = _setup(tmp_path)
    options = RunOptions(workflow_dirs=base.workflow_dirs, run_root=tmp_path, max_agents=1)
    source = """
await workflow('child', {who: 'x'});
try { await agent('b') } catch (e) { return e.message }
"""
    result = await _run(source, node_path, FakeDispatcher(), options)
    assert result == "agent() limit reached: at most 1 agent() calls per run"


async def test_workflow_call_nested_throws(node_path: str, tmp_path: Path) -> None:
    message = await _catch(
        "workflow('nested')",
        node_path,
        tmp_path,
        nested="return await workflow('child', {who: 'deep'})",
    )
    assert message == NEST_MESSAGE


async def test_workflow_call_unknown_name(node_path: str, tmp_path: Path) -> None:
    assert await _catch("workflow('nope')", node_path, tmp_path) == "unknown workflow: 'nope'"


async def test_workflow_call_outside_root(node_path: str, tmp_path: Path) -> None:
    message = await _catch("workflow({scriptPath: '../x.js'})", node_path, tmp_path)
    assert message.startswith("workflow path is outside the run root")


async def test_workflow_call_missing_path(node_path: str, tmp_path: Path) -> None:
    message = await _catch("workflow({scriptPath: 'wf/none.js'})", node_path, tmp_path)
    assert "cannot read workflow" in message


async def test_workflow_call_syntax_error(node_path: str, tmp_path: Path) -> None:
    message = await _catch("workflow('broken')", node_path, tmp_path, broken="return (")
    assert message.startswith("workflow ")


async def test_workflow_call_import_in_child(node_path: str, tmp_path: Path) -> None:
    message = await _catch("workflow('imp')", node_path, tmp_path, imp="import x from 'y'")
    assert "import/export is not allowed" in message


async def test_workflow_call_child_throw(node_path: str, tmp_path: Path) -> None:
    message = await _catch(
        "workflow('boom')", node_path, tmp_path, boom="throw new Error('child boom')"
    )
    assert message == "child boom"


async def test_workflow_call_undefined_is_null(node_path: str, tmp_path: Path) -> None:
    _, options = _setup(tmp_path, empty="")
    result = await _run("return await workflow('empty')", node_path, FakeDispatcher(), options)
    assert result is None


async def test_workflow_call_bad_ref(node_path: str, tmp_path: Path) -> None:
    assert await _catch("workflow(3)", node_path, tmp_path) == (
        "workflow() needs a name or {scriptPath}"
    )


async def test_workflow_call_no_run_root(node_path: str, tmp_path: Path) -> None:
    source = "try { await workflow({scriptPath: 'wf/child.js'}) } catch (e) { return e.message }"
    result = await _run(source, node_path, FakeDispatcher(), RunOptions())
    assert result == "workflow({scriptPath}) needs a run root"


async def test_workflow_call_shares_abort(node_path: str, tmp_path: Path) -> None:
    _, options = _setup(tmp_path, hang="return await agent('hang')")
    d = FakeDispatcher()
    signal = AbortSignal()
    task = asyncio.create_task(
        _run("return await workflow('hang')", node_path, d, options, abort=signal)
    )
    await asyncio.wait_for(d.wait_started("hang"), 30)
    signal.abort()
    assert await task is None
    assert d.cancelled == [0]


async def test_workflow_call_no_host_escape(node_path: str, tmp_path: Path) -> None:
    esc = (
        "return [typeof process, this === undefined ? 'u' : "
        "typeof this.constructor.constructor('return typeof process')()]"
    )
    _, options = _setup(tmp_path, esc=esc)
    result = await _run("return await workflow('esc')", node_path, FakeDispatcher(), options)
    assert "function" not in result
    assert "object" not in result


@pytest.mark.parametrize(
    "reach",
    [
        "globalThis.workflow",
        "Function('return this')().workflow",
        "Function('return workflow')()",
        # Values and functions from shared hooks must not lead back to the parent's realm.
        "(await parallel([() => 1])).constructor.constructor('return workflow')()",
        "(await pipeline([1], (x) => x)).constructor.constructor('return workflow')()",
        "(await agent.constructor('return workflow')())",
        "phase.constructor('return workflow')()",
        "budget.spent.constructor('return workflow')()",
    ],
)
async def test_workflow_call_nesting_cannot_be_bypassed(
    node_path: str, tmp_path: Path, reach: str
) -> None:
    message = await _catch(
        "workflow('nested')",
        node_path,
        tmp_path,
        nested=f"return await ({reach})('child', {{who: 'deep'}})",
    )
    assert message == NEST_MESSAGE


async def test_workflow_call_children_in_parallel(node_path: str, tmp_path: Path) -> None:
    _, options = _setup(tmp_path)
    source = (
        "return await parallel([() => workflow('child', {who: 'a'}),"
        " () => workflow('child', {who: 'b'})])"
    )
    result = await _run(source, node_path, FakeDispatcher(), options)
    assert [r["reply"] for r in result] == ["child a", "child b"]


async def test_workflow_call_child_has_own_globals(node_path: str, tmp_path: Path) -> None:
    _, options = _setup(tmp_path, leak="globalThis.leaked = 1; return typeof parentOnly")
    source = "globalThis.parentOnly = 1\nreturn [await workflow('leak'), typeof leaked]"
    assert await _run(source, node_path, FakeDispatcher(), options) == ["undefined", "undefined"]


@pytest.mark.parametrize(
    "probe",
    [
        "args.constructor.constructor('return typeof process')()",
        "agent.constructor('return typeof process')()",
        "budget.constructor.constructor('return typeof process')()",
        "typeof RealDate",
    ],
)
async def test_workflow_call_child_cannot_reach_host(
    node_path: str, tmp_path: Path, probe: str
) -> None:
    _, options = _setup(tmp_path, probe=f"return {probe}")
    result = await _run("return await workflow('probe', {})", node_path, FakeDispatcher(), options)
    assert result == "undefined"


async def test_workflow_call_child_bans_apply(node_path: str, tmp_path: Path) -> None:
    message = await _catch("workflow('clock')", node_path, tmp_path, clock="return Date.now()")
    assert "Date.now() is not allowed" in message


async def test_workflow_call_client_child_replays_from_snapshot(
    node_path: str, tmp_path: Path
) -> None:
    st = tmp_path / "st"
    client = ClientWorkflow("x", "return await agent('client child')", "virtual://c/workflows/x.js")
    first = await _run(
        "return await workflow('x')",
        node_path,
        FakeDispatcher(),
        RunOptions(state_dir=st, run_id="first", client_workflows=(client,)),
    )
    assert first == "client child"
    # The client is gone on resume: the journal's snapshot serves the child.
    dispatcher = FakeDispatcher()
    second = await _run(
        None,
        node_path,
        dispatcher,
        RunOptions(state_dir=st, run_id="second", resume_from="first"),
    )
    assert second == "client child"
    assert dispatcher.calls == []
