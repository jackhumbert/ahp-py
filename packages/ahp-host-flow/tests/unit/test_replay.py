"""Replay cursor and JournalRun, fresh and resumed (no Node)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahp_host_flow.engine import (
    AgentCall,
    AgentRecord,
    JournalError,
    JournalRun,
    JournalWriter,
    Replay,
    ResolvedScript,
    RunHeader,
    RunOptions,
    WorkflowSnapshot,
    call_key,
    load_journal,
    run_dir,
)


def _call(ordinal: int, prompt: str = "") -> AgentCall:
    return AgentCall(ordinal, prompt or f"p{ordinal}")


def _rec(ordinal: int, prompt: str = "") -> AgentRecord:
    return AgentRecord(ordinal, call_key(_call(ordinal, prompt)), "ok", f"v{ordinal}", 1)


def test_replay_full_match() -> None:
    logs: list[str] = []
    replay = Replay([_rec(2), _rec(0), _rec(1)], on_log=logs.append)
    for i in range(3):
        rec = replay.lookup(_call(i))
        assert rec is not None
        assert rec.ordinal == i
    assert replay.served == (0, 1, 2)
    assert not replay.live
    assert replay.ended_at is None
    assert logs == []


def test_replay_key_mismatch_ends_cache() -> None:
    logs: list[str] = []
    replay = Replay([_rec(0), _rec(1), _rec(2)], on_log=logs.append)
    assert replay.lookup(_call(0)) is not None
    assert replay.lookup(_call(1, "changed")) is None
    assert logs == [
        "resume: cache ended at agent() #1 (no cached call matches); "
        "1 calls replayed, this call and later ones run live"
    ]
    assert replay.live
    assert replay.ended_at == 1
    # Record 2 would match, but only the unchanged prefix is replayed.
    assert replay.lookup(_call(2)) is None
    assert replay.served == (0,)
    assert len(logs) == 1


def test_replay_gap_misses() -> None:
    logs: list[str] = []
    replay = Replay([_rec(1)], on_log=logs.append)
    assert replay.lookup(_call(0)) is None
    assert "(no cached call matches)" in logs[0]
    assert replay.ended_at == 0


def test_replay_records_exhausted() -> None:
    logs: list[str] = []
    replay = Replay([_rec(0)], on_log=logs.append)
    assert replay.lookup(_call(0)) is not None
    assert replay.lookup(_call(1)) is None
    assert logs == [
        "resume: cache ended at agent() #1 (no cached call matches); "
        "1 calls replayed, this call and later ones run live"
    ]


def _old_run(state: Path, run_id: str = "old", source: str | None = "return 1") -> None:
    header = RunHeader(run_id, "s.js", source, True, {"x": 1}, 7, 99)
    w = JournalWriter.create(state, header)
    w.append_agent(_rec(0))
    w.close()


def test_journal_run_fresh(tmp_path: Path) -> None:
    opts = RunOptions(state_dir=tmp_path, max_agents=5, budget_total=10)
    run = JournalRun.open(opts, source="return 2", filename="a.js", has_args=True, args=[1])
    run.close()
    assert len(run.run_id) == 32
    assert run.replay is None
    header = load_journal(tmp_path, run.run_id).header
    assert (header.source, header.filename, header.args, header.has_args) == (
        "return 2",
        "a.js",
        [1],
        True,
    )
    assert (header.max_agents, header.budget_total, header.resumed_from) == (5, 10, None)


def test_journal_run_fresh_needs_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="script source"):
        JournalRun.open(
            RunOptions(state_dir=tmp_path), source=None, filename="", has_args=False, args=None
        )


def test_journal_run_needs_state_dir() -> None:
    with pytest.raises(ValueError, match="state_dir"):
        JournalRun.open(RunOptions(), source="x", filename="", has_args=False, args=None)


def test_journal_run_resume_uses_old_snapshot(tmp_path: Path) -> None:
    _old_run(tmp_path)
    opts = RunOptions(state_dir=tmp_path, resume_from="old", run_id="new", max_agents=3)
    run = JournalRun.open(opts, source=None, filename="", has_args=False, args=None)
    run.close()
    assert run.run_id == "new"
    assert run.replay is not None
    assert (run.source, run.filename, run.has_args, run.args) == (
        "return 1",
        "s.js",
        True,
        {"x": 1},
    )
    assert (run.options.max_agents, run.options.budget_total) == (7, 99)
    header = load_journal(tmp_path, "new").header
    assert header.resumed_from == "old"
    assert header.max_agents == 7
    assert header.args == {"x": 1}


def test_journal_run_resume_rejects_args(tmp_path: Path) -> None:
    _old_run(tmp_path)
    opts = RunOptions(state_dir=tmp_path, resume_from="old")
    with pytest.raises(ValueError, match="args come from the journal"):
        JournalRun.open(opts, source=None, filename="", has_args=True, args=1)


def test_journal_run_resume_without_snapshot(tmp_path: Path) -> None:
    d = run_dir(tmp_path, "old")
    d.mkdir(parents=True)
    line = {"type": "run", "version": 1, "runId": "old", "filename": "s.js", "options": {}}
    (d / "journal.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    opts = RunOptions(state_dir=tmp_path, resume_from="old")
    with pytest.raises(JournalError, match="no script snapshot"):
        JournalRun.open(opts, source=None, filename="", has_args=False, args=None)


def test_journal_run_resume_edited_source_allowed(tmp_path: Path) -> None:
    _old_run(tmp_path, source=None)
    opts = RunOptions(state_dir=tmp_path, resume_from="old")
    run = JournalRun.open(opts, source="return 3", filename="e.js", has_args=False, args=None)
    run.close()
    assert run.source == "return 3"
    assert run.filename == "e.js"


def test_journal_run_resume_into_existing_id(tmp_path: Path) -> None:
    _old_run(tmp_path)
    opts = RunOptions(state_dir=tmp_path, resume_from="old", run_id="old2")
    JournalRun.open(opts, source=None, filename="", has_args=False, args=None).close()
    with pytest.raises(JournalError, match="already exists"):
        JournalRun.open(opts, source=None, filename="", has_args=False, args=None)


def test_journal_run_snapshots(tmp_path: Path) -> None:
    w = JournalWriter.create(tmp_path, RunHeader("old", "s.js", "x", False, None, 7, None))
    w.append_workflow(WorkflowSnapshot({"scriptPath": "wf/c.js"}, "/r/wf/c.js", "child"))
    w.close()
    opts = RunOptions(state_dir=tmp_path, resume_from="old", run_id="new")
    run = JournalRun.open(opts, source=None, filename="", has_args=False, args=None)
    ref = {"scriptPath": "wf/c.js"}
    assert run.snapshot_for(ref) == ResolvedScript("child", "/r/wf/c.js")
    assert run.snapshot_for({"scriptPath": "other.js"}) is None
    resolved = ResolvedScript("child", "/r/wf/c.js")
    run.record_workflow(ref, resolved)
    run.record_workflow(dict(ref), resolved)
    run.close()
    lines = (run_dir(tmp_path, "new") / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert sum('"type":"workflow"' in ln for ln in lines) == 1
