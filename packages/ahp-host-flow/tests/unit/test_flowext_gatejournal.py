"""Gate journal records: round trip, masking, and replay on resume (plan §4.8)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import (
    AbortSignal,
    GateRecord,
    JournalError,
    JournalWriter,
    Redactor,
    RunHeader,
    RunOptions,
    gate_key,
    load_journal,
    redact_agent_record,
)
from ahp_host_flow.engine.journal import AgentRecord
from ahp_host_flow.sandbox import run_script

from ..fakes import FakeDispatcher, FakeGates  # noqa: TID252

JS_TWO = "const a = await ask('First?'); const b = await ask('hang second'); return {a, b}\n"
PY_TWO = (
    "async def main(args):\n"
    "    a = await ask('First?')\n"
    "    b = await ask('hang second')\n"
    "    return {'a': a, 'b': b}\n"
)
JS_APPROVE = "const ok = await approve('Delete?'); const h = await ask('hang'); return {ok, h}\n"
JS_ABORT = "return await ask('hang q')\n"

POLL_BOUND = 10.0


def _header(run_id: str = "r1") -> RunHeader:
    return RunHeader(run_id, "w.js", "return 1", False, None, 10, None)


def _journal_path(state: Path, run_id: str) -> Path:
    return state / "runs" / run_id / "journal.jsonl"


def test_flowext_gatejournal_records_round_trip(tmp_path: Path) -> None:
    key0, key1 = gate_key("ask", "a", {}), gate_key("ask", "b", {})
    writer = JournalWriter.create(tmp_path, _header())
    writer.append_gate(GateRecord(0, key0, "ask", "open"))
    writer.append_gate(GateRecord(0, key0, "ask", "answered", "x"))
    writer.append_gate(GateRecord(1, key1, "ask", "open"))
    writer.close()
    path = _journal_path(tmp_path, "r1")
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write('{"type":"future"}\n')
    loaded = load_journal(tmp_path, "r1")
    assert loaded.gates == (
        GateRecord(0, key0, "ask", "answered", "x"),
        GateRecord(1, key1, "ask", "open"),
    )
    assert loaded.open_gates == (GateRecord(1, key1, "ask", "open"),)

    # An open line after an answered one does not downgrade it.
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(
            json.dumps(
                {
                    "type": "gate",
                    "ordinal": 0,
                    "key": key0,
                    "kind": "ask",
                    "status": "open",
                    "answer": None,
                }
            )
            + "\n"
        )
    assert load_journal(tmp_path, "r1").gates[0].status == "answered"

    bad = JournalWriter.create(tmp_path, _header("r2"))
    bad.close()
    with _journal_path(tmp_path, "r2").open("a", encoding="utf-8", newline="\n") as f:
        f.write(
            json.dumps(
                {"type": "gate", "ordinal": 0, "key": key0, "kind": "ask", "status": "maybe"}
            )
            + "\n"
        )
    with pytest.raises(JournalError, match="bad gate record"):
        load_journal(tmp_path, "r2")


def test_flowext_gatejournal_masked(tmp_path: Path) -> None:
    writer = JournalWriter.create(tmp_path, _header(), mask=Redactor().mask_value)
    writer.append_gate(
        GateRecord(0, gate_key("ask", "q", {}), "ask", "answered", "token=abcdefgh12345678")
    )
    writer.close()
    text = _journal_path(tmp_path, "r1").read_text(encoding="utf-8")
    assert "abcdefgh12345678" not in text
    assert "token=[redacted]" in text


async def _wait_journal(state: Path, run_id: str, *, answered: int, open_: int) -> None:
    """Poll until the journal shows `answered` answered gates and `open_` open ones."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + POLL_BOUND
    while loop.time() < deadline:
        try:
            loaded = load_journal(state, run_id)
        except JournalError:
            loaded = None
        if (
            loaded is not None
            and len(loaded.gates) - len(loaded.open_gates) >= answered
            and len(loaded.open_gates) >= open_
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("gate records did not appear in the journal")


async def _cut_off(
    source: str, filename: str, node: str | None, state: Path, gates: FakeGates
) -> None:
    task = asyncio.ensure_future(
        run_script(
            source,
            node=node,
            dispatcher=FakeDispatcher(),
            gates=gates,
            filename=filename,
            options=RunOptions(timeout=60, state_dir=state, run_id="first"),
        )
    )
    try:
        await _wait_journal(state, "first", answered=1, open_=1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _resume(
    node: str | None, state: Path, run_id: str, gates: FakeGates, *, continue_run: bool
) -> tuple[Any, list[str]]:
    logs: list[str] = []
    result = await asyncio.wait_for(
        run_script(
            None,
            node=node,
            dispatcher=FakeDispatcher(),
            gates=gates,
            options=RunOptions(
                timeout=60,
                state_dir=state,
                run_id=run_id,
                resume_from="first",
                continue_run=continue_run,
            ),
            on_log=logs.append,
        ),
        30,
    )
    return result, logs


async def _kill_resume(source: str, filename: str, node: str | None, tmp_path: Path) -> None:
    state = tmp_path / "state"
    await _cut_off(source, filename, node, state, FakeGates(answers={"First?": "one"}))
    copy = tmp_path / "state-copy"
    shutil.copytree(state, copy)
    for run_id, root, continue_run in (("second", state, False), ("third", copy, True)):
        gates = FakeGates(answers={"hang second": "two"}, hang=False)
        result, logs = await _resume(node, root, run_id, gates, continue_run=continue_run)
        assert result == {"a": "one", "b": "two"}
        assert [c.question for c in gates.calls] == ["hang second"]
        assert "resume: ask() gate #1 was still open; it is asked again" in logs
        records = load_journal(root, run_id).gates
        assert [(g.ordinal, g.status) for g in records] == [(0, "answered"), (1, "answered")]
        assert [g.answer for g in records] == ["one", "two"]


@pytest.mark.node
async def test_flowext_gatejournal_kill_resume_js(node_path: str, tmp_path: Path) -> None:
    await _kill_resume(JS_TWO, "w.js", node_path, tmp_path)


async def test_flowext_gatejournal_kill_resume_py(tmp_path: Path) -> None:
    await _kill_resume(PY_TWO, "w.py", None, tmp_path)


@pytest.mark.node
async def test_flowext_gatejournal_approve_replays_denial(node_path: str, tmp_path: Path) -> None:
    state = tmp_path / "state"
    await _cut_off(JS_APPROVE, "w.js", node_path, state, FakeGates())
    assert load_journal(state, "first").gates[0].answer is False
    gates = FakeGates(hang=False, answers={"hang": "h"})
    result, _ = await _resume(node_path, state, "second", gates, continue_run=False)
    assert [c.question for c in gates.calls] == ["hang"]
    assert result["ok"] is False
    assert result["h"] == "h"


@pytest.mark.node
async def test_flowext_gatejournal_abort_leaves_open(node_path: str, tmp_path: Path) -> None:
    state = tmp_path / "state"
    gates = FakeGates()
    abort = AbortSignal()
    task = asyncio.ensure_future(
        run_script(
            JS_ABORT,
            node=node_path,
            dispatcher=FakeDispatcher(),
            gates=gates,
            abort=abort,
            options=RunOptions(timeout=60, state_dir=state, run_id="first"),
        )
    )
    await asyncio.wait_for(gates.wait_asked("hang q"), 30)
    abort.abort()
    assert await asyncio.wait_for(task, 30) is None
    loaded = load_journal(state, "first")
    assert [(g.ordinal, g.status) for g in loaded.gates] == [(0, "open")]


def test_flowext_gatejournal_redact_keeps_gate_line(tmp_path: Path) -> None:
    key = gate_key("ask", "q", {})
    writer = JournalWriter.create(tmp_path, _header())
    writer.append_agent(AgentRecord(0, "k", "ok", "secret value"))
    writer.append_gate(GateRecord(0, key, "ask", "open"))
    writer.append_gate(GateRecord(0, key, "ask", "answered", "an answer"))
    writer.close()
    path = _journal_path(tmp_path, "r1")

    def gate_lines() -> list[bytes]:
        return [ln for ln in path.read_bytes().split(b"\n") if b'"type":"gate"' in ln]

    before = gate_lines()
    assert len(before) == 2
    redact_agent_record(tmp_path, "r1", 0, reason="test", redacted_at="2026-01-01T00:00:00Z")
    assert gate_lines() == before
