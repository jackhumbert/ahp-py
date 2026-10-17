"""Run each conformance script against a fake dispatcher and compare its sidecar."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import MAX_AGENTS_PER_RUN, RunOptions
from ahp_host_flow.sandbox import NO_ARGS, extract_meta, run_script

from ..fakes import FakeDispatcher  # noqa: TID252

pytestmark = pytest.mark.node

CONFORMANCE = Path(__file__).resolve().parents[2] / "conformance"
# Top level only: workflows/ holds children that the scripts call.
SCRIPTS = sorted(CONFORMANCE.glob("*.js"))

ROWS = {
    "meta",
    "agent",
    "parallel",
    "pipeline",
    "phase_log_args",
    "budget",
    "budget_unset",
    "workflow",
    "determinism",
    "caps",
    "caps_concurrency",
}


def _options(spec: dict[str, Any]) -> RunOptions:
    return RunOptions(
        concurrency=spec.get("concurrency"),
        max_agents=spec.get("max_agents", MAX_AGENTS_PER_RUN),
        budget_total=spec.get("budget_total"),
        timeout=spec.get("timeout", 60),
        workflow_dirs=tuple(CONFORMANCE / d for d in spec.get("workflow_dirs", [])),
        run_root=CONFORMANCE,
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.stem)
async def test_conformance_script(node_path: str, script: Path) -> None:
    sidecar_path = script.with_suffix(".json")
    assert sidecar_path.is_file(), f"{script.name} has no {sidecar_path.name}"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    source = script.read_text(encoding="utf-8")
    dispatcher = FakeDispatcher(**sidecar.get("dispatcher", {}))
    logs: list[str] = []
    phases: list[str] = []
    value = await asyncio.wait_for(
        run_script(
            source,
            node=node_path,
            dispatcher=dispatcher,
            args=sidecar.get("args", NO_ARGS),
            filename=str(script),
            options=_options(sidecar.get("options", {})),
            on_log=logs.append,
            on_phase=phases.append,
        ),
        120,
    )
    assert value == sidecar["result"]
    if "meta" in sidecar:
        assert await extract_meta(source, node=node_path) == sidecar["meta"]
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
    assert {p.stem for p in SCRIPTS} >= ROWS
    for script in SCRIPTS:
        assert script.with_suffix(".json").is_file(), script.name
