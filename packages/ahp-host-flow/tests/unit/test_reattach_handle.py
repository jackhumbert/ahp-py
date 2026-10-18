"""Start records carry the node session; a continued run hands it back (no Node)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ahp_host_flow.engine import (
    AgentCall,
    AgentRecord,
    AgentResult,
    DispatchHandle,
    JournalError,
    JournalRun,
    JournalWriter,
    Replay,
    RunHeader,
    RunOptions,
    Scheduler,
    StartRecord,
    call_key,
    load_journal,
    reattach_handle,
    run_dir,
)


def _writer(tmp_path: Path, run_id: str = "r1") -> JournalWriter:
    return JournalWriter.create(tmp_path, RunHeader(run_id, "w.js", "x", False, None, 10, None))


def test_reattach_handle_start_record_round_trips(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.append_started(StartRecord(0, "k0", "fleet", "chat:/w1", "claude:/s1"))
    writer.append_started(StartRecord(1, "k1", "p", "chat:/w2"))
    writer.close()
    loaded = load_journal(tmp_path, "r1")
    assert loaded.started == (
        StartRecord(0, "k0", "fleet", "chat:/w1", "claude:/s1"),
        StartRecord(1, "k1", "p", "chat:/w2"),
    )
    lines = (run_dir(tmp_path, "r1") / "journal.jsonl").read_text("utf-8").splitlines()
    assert json.loads(lines[1])["handle"]["session"] == "claude:/s1"
    assert "session" not in json.loads(lines[2])["handle"]


def test_reattach_handle_bad_session_is_a_journal_error(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.close()
    path = run_dir(tmp_path, "r1") / "journal.jsonl"
    bad = {"type": "started", "ordinal": 0, "key": "k", "handle": {"session": 5}}
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(bad) + "\n")
    with pytest.raises(JournalError, match="journal line 2: bad started record"):
        load_journal(tmp_path, "r1")


class _Seeing:
    def __init__(self) -> None:
        self.seen: dict[str, DispatchHandle | None] = {}

    async def run_agent(self, call: AgentCall) -> AgentResult:
        self.seen[call.prompt] = reattach_handle()
        return AgentResult(value=call.prompt)


async def test_reattach_handle_reaches_the_dispatcher_of_the_matching_call() -> None:
    a, b = AgentCall(0, "a"), AgentCall(1, "b")
    replay = Replay(
        [AgentRecord(0, call_key(a), "ok", "a")],
        prefix_only=False,
        unfinished=[StartRecord(1, call_key(b), "fleet", "chat:/w1", "claude:/s1")],
    )
    dispatcher = _Seeing()
    scheduler = Scheduler(dispatcher, RunOptions(), replay=replay)
    issued = [scheduler.issue(p, {}) for p in ("a", "b", "c")]
    assert issued[0][1] is not None
    assert issued[1][1] is None
    assert issued[2][1] is None
    await asyncio.wait_for(
        asyncio.gather(*(scheduler.run(call, cached) for call, cached in issued)), 5
    )
    assert "a" not in dispatcher.seen
    assert dispatcher.seen["b"] == DispatchHandle("fleet", "chat:/w1", "claude:/s1")
    assert dispatcher.seen["c"] is None
    assert reattach_handle() is None


def test_reattach_handle_continue_run_logs_the_reattach(tmp_path: Path) -> None:
    writer = _writer(tmp_path, "old")
    writer.append_started(StartRecord(0, "k0", "fleet", "chat:/w1", "claude:/s1"))
    writer.append_started(StartRecord(1, "k1", "p", "chat:/w2"))
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
        "resume: agent() #0 started on fleet (chat:/w1) in session claude:/s1 but was cut off "
        "by a host restart; its dispatcher reattaches or dispatches it again",
        "resume: agent() #1 started on p (chat:/w2) but was cut off by a host restart; "
        "dispatching it again",
    ]
