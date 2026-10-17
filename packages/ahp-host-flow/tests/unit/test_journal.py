"""Journal record format, call key, writer and torn-tolerant reader (no Node)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ahp_host_flow.engine import (
    RUN_ID,
    AgentCall,
    AgentRecord,
    JournalError,
    JournalWriter,
    RunHeader,
    WorkflowSnapshot,
    call_key,
    canonical_json,
    load_journal,
    new_run_id,
    run_dir,
)


def _header(run_id: str = "r1") -> RunHeader:
    return RunHeader(
        run_id=run_id,
        filename="s.js",
        source="return 1",
        has_args=True,
        args={"a": [1, 2]},
        max_agents=1000,
        budget_total=50,
        resumed_from="r0",
    )


def test_journal_canonical_json() -> None:
    assert canonical_json({"b": 1, "a": [1, "é"]}) == '{"a":[1,"é"],"b":1}'


def test_journal_call_key() -> None:
    base = AgentCall(0, "p", {"a": 1, "b": 2}, "ph", "lb")
    assert call_key(base) == call_key(AgentCall(0, "p", {"b": 2, "a": 1}, "ph", "lb"))
    expected = hashlib.sha256(
        canonical_json(
            {"label": "lb", "opts": {"a": 1, "b": 2}, "phase": "ph", "prompt": "p"}
        ).encode()
    ).hexdigest()
    assert call_key(base) == expected
    for other in (
        AgentCall(0, "q", {"a": 1, "b": 2}, "ph", "lb"),
        AgentCall(0, "p", {"a": 1, "b": 3}, "ph", "lb"),
        AgentCall(0, "p", {"a": 1, "b": 2}, "px", "lb"),
        AgentCall(0, "p", {"a": 1, "b": 2}, "ph", "lx"),
    ):
        assert call_key(other) != call_key(base)


def test_journal_run_id() -> None:
    rid = new_run_id()
    assert RUN_ID.fullmatch(rid)
    assert len(rid) == 32
    with pytest.raises(ValueError, match="invalid run id"):
        run_dir(Path("x"), "../x")


def test_journal_round_trip(tmp_path: Path) -> None:
    header = _header()
    w = JournalWriter.create(tmp_path, header)
    r1 = AgentRecord(1, "k1", "error", None, 0, "boom")
    r0 = AgentRecord(0, "k0", "ok", {"x": "é"}, 7)
    w.append_agent(r1)
    w.append_agent(r0)
    snap = WorkflowSnapshot({"scriptPath": "c.js"}, "c.js", "return 2")
    w.append_workflow(snap)
    w.close()
    assert b"\r" not in w.path.read_bytes()
    loaded = load_journal(tmp_path, "r1")
    assert loaded.agents == (r0, r1)
    assert loaded.workflows == {canonical_json({"scriptPath": "c.js"}): snap}
    assert loaded.header == header


def test_journal_create_twice(tmp_path: Path) -> None:
    JournalWriter.create(tmp_path, _header()).close()
    with pytest.raises(JournalError, match="already exists"):
        JournalWriter.create(tmp_path, _header())


def test_journal_torn_tail_ignored(tmp_path: Path) -> None:
    w = JournalWriter.create(tmp_path, _header())
    w.append_agent(AgentRecord(0, "k", "null", None))
    w.close()
    with w.path.open("ab") as f:
        f.write(b'{"type":"agent","ord')
    assert len(load_journal(tmp_path, "r1").agents) == 1


def test_journal_invalid_middle_line(tmp_path: Path) -> None:
    w = JournalWriter.create(tmp_path, _header())
    w.close()
    with w.path.open("ab") as f:
        f.write(b"not json\n" + json.dumps({"type": "workflow"}).encode() + b"\n")
    with pytest.raises(JournalError, match="line 2"):
        load_journal(tmp_path, "r1")


def test_journal_missing(tmp_path: Path) -> None:
    with pytest.raises(JournalError, match="no journal"):
        load_journal(tmp_path, "nope")


def test_journal_first_line_not_header(tmp_path: Path) -> None:
    d = run_dir(tmp_path, "r2")
    d.mkdir(parents=True)
    (d / "journal.jsonl").write_text('{"type":"agent"}\n', encoding="utf-8")
    with pytest.raises(JournalError, match="run header"):
        load_journal(tmp_path, "r2")


def test_journal_append_after_close_is_noop(tmp_path: Path) -> None:
    w = JournalWriter.create(tmp_path, _header())
    w.close()
    before = w.path.read_bytes()
    w.append_agent(AgentRecord(0, "k", "ok", 1))
    w.close()
    assert w.path.read_bytes() == before
