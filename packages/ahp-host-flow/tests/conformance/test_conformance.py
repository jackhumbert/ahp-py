"""Run each conformance script against a fake dispatcher and compare its sidecar."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import MAX_AGENTS_PER_RUN, JournalError, RunOptions, load_journal
from ahp_host_flow.sandbox import (
    MIN_NODE_MAJOR,
    NO_ARGS,
    NODE_ENV,
    extract_meta,
    find_node,
    run_script,
)

from ..fakes import FakeDispatcher  # noqa: TID252

CONFORMANCE = Path(__file__).resolve().parents[2] / "conformance"
# Python twins that call a JS child, so they need Node as well.
PY_NEEDS_NODE = {"workflow"}


def _param(path: Path) -> Any:
    needs_node = path.suffix == ".js" or path.stem in PY_NEEDS_NODE
    return pytest.param(
        path,
        id=f"{path.suffix[1:]}-{path.stem}",
        marks=pytest.mark.node if needs_node else (),
    )


# Top level only: workflows/ holds children that the scripts call.
SCRIPTS = [_param(p) for suffix in (".js", ".py") for p in sorted(CONFORMANCE.glob(f"*{suffix}"))]

ROWS = {
    "meta",
    "agent",
    "agent_schema",
    "parallel",
    "pipeline",
    "phase_log_args",
    "budget",
    "budget_unset",
    "nonfinite",
    "workflow",
    "determinism",
    "caps",
    "caps_concurrency",
    "resume",
}


def _node_for(script: Path) -> str | None:
    """The Node path for a script that runs under Node (or calls into it); None otherwise."""
    if script.suffix == ".py" and script.stem not in PY_NEEDS_NODE:
        return None
    path = find_node()
    if path is None:
        pytest.skip(f"no Node >= {MIN_NODE_MAJOR} (set {NODE_ENV} or put node on PATH)")
    return path


def _expected(script: Path, sidecar: dict[str, Any]) -> Any:
    if script.suffix == ".py" and "py_result" in sidecar:
        return sidecar["py_result"]
    return sidecar["result"]


def _options(
    spec: dict[str, Any],
    *,
    state_dir: Path | None = None,
    run_id: str | None = None,
    resume_from: str | None = None,
) -> RunOptions:
    return RunOptions(
        state_dir=state_dir,
        run_id=run_id,
        resume_from=resume_from,
        concurrency=spec.get("concurrency"),
        max_agents=spec.get("max_agents", MAX_AGENTS_PER_RUN),
        budget_total=spec.get("budget_total"),
        timeout=spec.get("timeout", 60),
        workflow_dirs=tuple(CONFORMANCE / d for d in spec.get("workflow_dirs", [])),
        run_root=CONFORMANCE,
    )


async def _check_resume(
    node: str | None, script: Path, sidecar: dict[str, Any], tmp_path: Path
) -> None:
    source = script.read_text(encoding="utf-8")
    spec = sidecar.get("options", {})
    resume = sidecar["resume"]
    dispatcher_kwargs = sidecar.get("dispatcher", {})
    args = sidecar.get("args", NO_ARGS)
    state = tmp_path / "state"

    # (1) Uninterrupted run.
    expected = _expected(script, sidecar)
    value = await run_script(
        source,
        node=node,
        dispatcher=FakeDispatcher(**dispatcher_kwargs, hang=False),
        args=args,
        filename=str(script),
        options=_options(spec),
    )
    assert value == expected

    # (2) A run killed with calls in flight.
    killed = FakeDispatcher(**dispatcher_kwargs)
    task = asyncio.ensure_future(
        run_script(
            source,
            node=node,
            dispatcher=killed,
            args=args,
            filename=str(script),
            options=_options(spec, state_dir=state, run_id="first"),
        )
    )
    try:
        await asyncio.wait_for(
            asyncio.gather(*(killed.wait_started(p) for p in resume["kill_when_started"])), 30
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        recorded = 0
        while recorded < resume["kill_after_records"] and loop.time() < deadline:
            try:
                recorded = len(load_journal(state, "first").agents)
            except JournalError:
                recorded = 0  # the journal is not there yet
            await asyncio.sleep(0.01)
        assert recorded >= resume["kill_after_records"]
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # (3) Resume it from its journal.
    resumed = FakeDispatcher(**dispatcher_kwargs, hang=False)
    logs: list[str] = []
    value = await run_script(
        None,
        node=node,
        dispatcher=resumed,
        args=args,
        filename=str(script),
        options=_options(spec, state_dir=state, run_id="second", resume_from="first"),
        on_log=logs.append,
    )
    assert value == expected
    if "unfinished" in resume:
        assert len(load_journal(state, "first").unfinished) == resume["unfinished"]
        assert sum("never finished" in line for line in logs) == resume["unfinished"]
    assert sorted(c.prompt for c in resumed.calls) == resume["live"]
    journaled = {r.ordinal for r in load_journal(state, "second").agents}
    assert sorted(journaled - {c.ordinal for c in resumed.calls}) == resume["cached"]


@pytest.mark.parametrize("script", SCRIPTS)
async def test_conformance_script(script: Path, tmp_path: Path) -> None:
    node = _node_for(script)
    sidecar_path = script.with_suffix(".json")
    assert sidecar_path.is_file(), f"{script.name} has no {sidecar_path.name}"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if "resume" in sidecar:
        await _check_resume(node, script, sidecar, tmp_path)
        return
    source = script.read_text(encoding="utf-8")
    dispatcher = FakeDispatcher(**sidecar.get("dispatcher", {}))
    logs: list[str] = []
    phases: list[str] = []
    value = await asyncio.wait_for(
        run_script(
            source,
            node=node,
            dispatcher=dispatcher,
            args=sidecar.get("args", NO_ARGS),
            filename=str(script),
            options=_options(sidecar.get("options", {})),
            on_log=logs.append,
            on_phase=phases.append,
        ),
        120,
    )
    assert value == _expected(script, sidecar)
    if "meta" in sidecar:
        assert await extract_meta(source, node=node, filename=str(script)) == sidecar["meta"]
    if "logs" in sidecar:
        assert logs == sidecar["logs"]
    if "phases" in sidecar:
        assert phases == sidecar["phases"]
    if "calls" in sidecar:
        calls = sorted(dispatcher.calls, key=lambda c: c.ordinal)
        assert [[c.ordinal, c.prompt, c.phase, c.label] for c in calls] == sidecar["calls"]
    if "max_in_flight" in sidecar:
        assert dispatcher.max_in_flight == sidecar["max_in_flight"]


def test_conformance_rows_covered() -> None:
    js = {p.stem for p in CONFORMANCE.glob("*.js")}
    py = {p.stem for p in CONFORMANCE.glob("*.py")}
    assert js >= ROWS
    assert js == py
    sidecars = {}
    for stem in js:
        path = (CONFORMANCE / stem).with_suffix(".json")
        assert path.is_file(), path.name
        sidecars[stem] = json.loads(path.read_text(encoding="utf-8"))
    assert {stem for stem, sc in sidecars.items() if "py_result" in sc} == {"determinism"}
