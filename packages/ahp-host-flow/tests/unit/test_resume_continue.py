"""RunOptions.continue_run: replay by key after a miss, cut-off log lines (no Node)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host_flow.engine import (
    AgentCall,
    AgentRecord,
    JournalRun,
    JournalWriter,
    Replay,
    RunHeader,
    RunOptions,
    StartRecord,
    call_key,
    load_journal,
    run_dir,
)


def _record(call: AgentCall, ordinal: int) -> AgentRecord:
    return AgentRecord(ordinal, call_key(call), "ok", f"v{ordinal}")


def test_continue_run_replays_finished_calls_after_a_miss() -> None:
    a, b, c = AgentCall(0, "a"), AgentCall(1, "b"), AgentCall(2, "c")
    records = [_record(a, 0), _record(c, 1)]
    stray = AgentCall(1, "never recorded")
    later = AgentCall(3, "c")

    prefix = Replay(records)
    assert prefix.lookup(a) is not None
    assert prefix.lookup(stray) is None
    assert prefix.lookup(later) is None
    assert prefix.live

    by_key = Replay(records, prefix_only=False)
    assert by_key.lookup(a) is not None
    assert by_key.lookup(b) is None
    assert not by_key.live
    assert by_key.ended_at is None
    hit = by_key.lookup(later)
    assert hit == AgentRecord(3, call_key(c), "ok", "v1")
    assert not by_key.live


def test_continue_run_needs_resume_from(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="continue_run needs resume_from"):
        RunOptions(continue_run=True)
    with pytest.raises(ValueError, match="continue_run needs resume_from"):
        RunOptions(continue_run=True, state_dir=tmp_path)
    assert RunOptions(continue_run=True, state_dir=tmp_path, resume_from="r1").continue_run
    with pytest.raises(ValueError, match="continue_run must be a bool"):
        RunOptions(continue_run=1)  # type: ignore[arg-type]


def test_continue_run_logs_each_cut_off_call(tmp_path: Path) -> None:
    writer = JournalWriter.create(tmp_path, RunHeader("old", "w.js", "x", False, None, 10, None))
    call = AgentCall(0, "p0")
    writer.append_agent(_record(call, 0))
    writer.append_started(StartRecord(1, "k1", "p", "chat:/w1"))
    writer.close()
    logs: list[str] = []
    run = JournalRun.open(
        RunOptions(state_dir=tmp_path, resume_from="old", continue_run=True),
        source=None,
        filename="w.js",
        has_args=False,
        args=None,
        on_log=logs.append,
    )
    run.close()
    assert logs == [
        "resume: agent() #1 started on p (chat:/w1) but was cut off by a host restart; "
        "dispatching it again"
    ]


def test_journal_ignores_an_unknown_record_type(tmp_path: Path) -> None:
    writer = JournalWriter.create(tmp_path, RunHeader("r1", "w.js", "x", False, None, 10, None))
    rec = _record(AgentCall(0, "p0"), 0)
    writer.append_agent(rec)
    writer.close()
    path = run_dir(tmp_path, "r1") / "journal.jsonl"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write('{"type":"gate","id":"g1","state":"open"}\n')
    assert load_journal(tmp_path, "r1").agents == (rec,)
