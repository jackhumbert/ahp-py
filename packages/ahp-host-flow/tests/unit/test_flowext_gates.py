"""ask(), approve() and canvas() on the wire: runner errors, abort, masking, shared counter."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow import verbs
from ahp_host_flow.engine import AbortSignal, Redactor, RunOptions, gate_key
from ahp_host_flow.sandbox import protocol, run_script

from ..fakes import FakeDispatcher, FakeGates  # noqa: TID252

RUNNER = Path(__file__).resolve().parents[2] / "src" / "ahp_host_flow" / "sandbox" / "runner.mjs"

JS_ABORT = "return await parallel([() => ask('hang a'), () => approve('hang b')])\n"
PY_ABORT = (
    "async def main(args):\n"
    "    return await parallel([lambda: ask('hang a'), lambda: approve('hang b')])\n"
)


def test_flowext_gates_protocol_codes_match() -> None:
    match = re.search(r"const GATE_ERROR = (-\d+);", RUNNER.read_text(encoding="utf-8"))
    assert match is not None
    assert int(match.group(1)) == protocol.GATE_ERROR


def test_flowext_gates_key_stable() -> None:
    options = {"choices": ["a", "b"]}
    assert gate_key("ask", "q", options) == gate_key("ask", "q", {"choices": ["a", "b"]})
    assert gate_key("ask", "q", options) != gate_key("ask", "q", {"choices": ["a", "c"]})
    assert gate_key("ask", "q", {}) != gate_key("approve", "q", {})


async def _abort_run(source: str, filename: str, node: str | None) -> tuple[Any, list[str]]:
    gates = FakeGates()
    abort = AbortSignal()
    logs: list[str] = []
    task = asyncio.ensure_future(
        run_script(
            source,
            node=node,
            dispatcher=FakeDispatcher(),
            gates=gates,
            abort=abort,
            filename=filename,
            options=RunOptions(timeout=60),
            on_log=logs.append,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(gates.wait_asked("hang a"), gates.wait_asked("hang b")), 30
    )
    abort.abort()
    return await asyncio.wait_for(task, 30), logs


@pytest.mark.node
async def test_flowext_gates_abort_js(node_path: str) -> None:
    result, logs = await _abort_run(JS_ABORT, "w.js", node_path)
    assert result == [None, False]
    assert sum("aborted" in line for line in logs) == 2


async def test_flowext_gates_abort_py() -> None:
    result, logs = await _abort_run(PY_ABORT, "w.py", None)
    assert result == [None, False]
    assert sum("aborted" in line for line in logs) == 2


async def test_flowext_gates_after_abort_returns_at_once() -> None:
    gates = FakeGates()
    abort = AbortSignal()
    abort.abort()
    result = await asyncio.wait_for(
        run_script(
            "async def main(args):\n    return await ask('q')\n",
            node=None,
            dispatcher=FakeDispatcher(),
            gates=gates,
            abort=abort,
            filename="w.py",
        ),
        30,
    )
    assert result is None
    assert gates.calls == []


async def test_flowext_gates_masked_answer() -> None:
    gates = FakeGates(answers={"Key?": "token=abcdefgh12345678"})
    result = await run_script(
        "async def main(args):\n    return await ask('Key?')\n",
        node=None,
        dispatcher=FakeDispatcher(),
        gates=gates,
        filename="w.py",
        options=RunOptions(redact=Redactor().mask_value),
    )
    assert result == "token=[redacted]"


@pytest.mark.node
async def test_flowext_gates_child_shares_counter(node_path: str, tmp_path: Path) -> None:
    (tmp_path / "child.py").write_text(
        "async def main(args):\n    return await ask('child?')\n", encoding="utf-8"
    )
    gates = FakeGates(answers={"parent?": "p", "child?": "c"})
    source = (
        "const p = await ask('parent?')\n"
        "const c = await workflow({scriptPath: 'child.py'})\n"
        "return [p, c]\n"
    )
    result = await run_script(
        source,
        node=node_path,
        dispatcher=FakeDispatcher(),
        gates=gates,
        filename="parent.js",
        options=RunOptions(run_root=tmp_path, timeout=60),
    )
    assert result == ["p", "c"]
    assert sorted((c.ordinal, c.question) for c in gates.calls) == [(0, "parent?"), (1, "child?")]


def test_flowext_gates_verbs_outside_runner() -> None:
    with pytest.raises(verbs.OutsideRunnerError):
        verbs.ask("q")
    with pytest.raises(verbs.OutsideRunnerError):
        verbs.approve("s")
    with pytest.raises(verbs.OutsideRunnerError):
        verbs.canvas("https://example.com")
