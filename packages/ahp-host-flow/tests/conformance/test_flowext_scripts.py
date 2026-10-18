"""Run each extension conformance script against fakes and compare its sidecar."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import RunOptions
from ahp_host_flow.sandbox import MIN_NODE_MAJOR, NODE_ENV, find_node, run_script

from ..fakes import FakeDispatcher, FakeGates  # noqa: TID252

EXTENSIONS = Path(__file__).resolve().parents[2] / "conformance" / "extensions"


def _param(path: Path) -> Any:
    return pytest.param(
        path,
        id=f"{path.suffix[1:]}-{path.stem}",
        marks=pytest.mark.node if path.suffix == ".js" else (),
    )


SCRIPTS = [_param(p) for suffix in (".js", ".py") for p in sorted(EXTENSIONS.glob(f"*{suffix}"))]


@pytest.mark.parametrize("script", SCRIPTS)
async def test_flowext_scripts_script(script: Path) -> None:
    node: str | None = None
    if script.suffix == ".js":
        node = find_node()
        if node is None:
            pytest.skip(f"no Node >= {MIN_NODE_MAJOR} (set {NODE_ENV} or put node on PATH)")
    sidecar = json.loads(script.with_suffix(".json").read_text(encoding="utf-8"))
    dispatcher = FakeDispatcher()
    gates = FakeGates(answers=sidecar.get("answers"), approvals=sidecar.get("approvals"))
    logs: list[str] = []
    value = await run_script(
        script.read_text(encoding="utf-8"),
        node=node,
        dispatcher=dispatcher,
        gates=None if sidecar.get("gates_off") else gates,
        filename=str(script),
        options=RunOptions(timeout=60),
        on_log=logs.append,
    )
    assert value == sidecar["result"]
    if "gates" in sidecar:
        got = sorted([c.ordinal, c.kind, c.question, dict(c.options)] for c in gates.calls)
        assert got == sidecar["gates"]
    if "calls" in sidecar:
        assert sorted([c.ordinal, c.prompt] for c in dispatcher.calls) == sidecar["calls"]
    if "canvases" in sidecar:
        assert [list(c) for c in gates.canvases] == sidecar["canvases"]
    for needle in sidecar.get("logs_contain", []):
        assert any(needle in line for line in logs), (needle, logs)


def test_flowext_scripts_twins() -> None:
    js = {p.stem for p in EXTENSIONS.glob("*.js")}
    py = {p.stem for p in EXTENSIONS.glob("*.py")}
    assert js == py
    for stem in js:
        assert (EXTENSIONS / f"{stem}.json").is_file(), stem
