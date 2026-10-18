"""Redacting a finished agent() record in the journal (plan §4.9, M9)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ahp_host_flow.engine import (
    JOURNAL_FILE,
    AgentRecord,
    EchoDispatcher,
    JournalBusyError,
    JournalError,
    JournalWriter,
    RunHeader,
    RunOptions,
    load_journal,
    redact_agent_record,
    run_dir,
)
from ahp_host_flow.library import run

SECRET = "PLAINLEAK-4242"
PROMPT = "say " + SECRET
AT = "2026-01-01T00:00:00.000Z"
MARKER = {"reason": "leak", "redactedAt": AT}


def _journal_text(state_dir: Path, run_id: str) -> str:
    return (run_dir(state_dir, run_id) / JOURNAL_FILE).read_text(encoding="utf-8")


async def _finished_run(state_dir: Path) -> str:
    async with run(EchoDispatcher(), RunOptions(state_dir=state_dir)) as f:
        await f.agent(PROMPT)
        assert f.run_id is not None
        return f.run_id


async def test_journal_redaction_rewrites_value_and_marks_record(tmp_path: Path) -> None:
    run_id = await _finished_run(tmp_path)
    assert SECRET in _journal_text(tmp_path, run_id)
    before = load_journal(tmp_path, run_id)

    record = redact_agent_record(tmp_path, run_id, 0, reason="leak", redacted_at=AT)

    assert record.value == "[redacted]"
    assert record.redacted == MARKER
    assert SECRET not in _journal_text(tmp_path, run_id)
    assert list(run_dir(tmp_path, run_id).glob("*.tmp")) == []
    after = load_journal(tmp_path, run_id)
    assert after.header == before.header
    assert after.started == before.started


async def test_journal_redaction_resume_replays_redacted_value(tmp_path: Path) -> None:
    run_id = await _finished_run(tmp_path)
    redact_agent_record(tmp_path, run_id, 0, reason="leak", redacted_at=AT)

    echo = EchoDispatcher()
    async with run(echo, RunOptions(state_dir=tmp_path, resume_from=run_id)) as g:
        w = await g.agent(PROMPT)
        assert g.run_id is not None
        new_id = g.run_id

    assert w == "[redacted]"
    assert echo.calls == []
    text = _journal_text(tmp_path, new_id)
    assert SECRET not in text
    (record,) = load_journal(tmp_path, new_id).agents
    assert record.value == "[redacted]"
    assert record.redacted == MARKER


def test_journal_redaction_error_record(tmp_path: Path) -> None:
    header = RunHeader("r1", "x.js", "src", False, None, 10, None)
    writer = JournalWriter.create(tmp_path, header)
    writer.append_agent(AgentRecord(0, "k", "error", None, error=SECRET))
    writer.close()

    record = redact_agent_record(tmp_path, "r1", 0, reason="leak", redacted_at=AT)

    assert record.error == "[redacted]"
    assert record.status == "error"
    assert SECRET not in _journal_text(tmp_path, "r1")


async def test_journal_redaction_bad_targets(tmp_path: Path) -> None:
    run_id = await _finished_run(tmp_path)
    with pytest.raises(JournalError, match="has no finished agent"):
        redact_agent_record(tmp_path, run_id, 7, reason="leak", redacted_at=AT)
    with pytest.raises(JournalError, match="no journal for run"):
        redact_agent_record(tmp_path, "nosuchrun", 0, reason="leak", redacted_at=AT)
    with pytest.raises(ValueError, match="non-negative"):
        redact_agent_record(tmp_path, run_id, -1, reason="leak", redacted_at=AT)
    assert SECRET in _journal_text(tmp_path, run_id)


async def test_journal_redaction_drops_torn_tail(tmp_path: Path) -> None:
    run_id = await _finished_run(tmp_path)
    path = run_dir(tmp_path, run_id) / JOURNAL_FILE
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write('{"type":"agent","ordinal":1,"value":"' + SECRET)

    redact_agent_record(tmp_path, run_id, 0, reason="leak", redacted_at=AT)

    text = _journal_text(tmp_path, run_id)
    assert SECRET not in text
    assert text.endswith("\n")
    assert [a.ordinal for a in load_journal(tmp_path, run_id).agents] == [0]


async def test_journal_redaction_malformed_marker_is_an_error(tmp_path: Path) -> None:
    run_id = await _finished_run(tmp_path)
    path = run_dir(tmp_path, run_id) / JOURNAL_FILE
    lines = path.read_text(encoding="utf-8").split("\n")[:-1]
    for i, line in enumerate(lines):
        rec = json.loads(line)
        if rec["type"] == "agent":
            rec["redacted"] = 5
            lines[i] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(JournalError, match="bad agent record"):
        load_journal(tmp_path, run_id)


def test_journal_redaction_refuses_a_journal_a_live_writer_holds(tmp_path: Path) -> None:
    header = RunHeader("live1", "x.js", "src", False, None, 10, None)
    writer = JournalWriter.create(tmp_path, header)
    writer.append_agent(AgentRecord(0, "k", "ok", SECRET))
    before = _journal_text(tmp_path, "live1")
    try:
        with pytest.raises(JournalBusyError, match="still being written"):
            redact_agent_record(tmp_path, "live1", 0, reason="leak", redacted_at=AT)
        assert _journal_text(tmp_path, "live1") == before
        # A second writer cannot take the same run either.
        with pytest.raises(JournalError):
            JournalWriter.create(tmp_path, header)
    finally:
        writer.close()
    # Closed: the lock is released, and a crashed run would be too (the OS drops it).
    redact_agent_record(tmp_path, "live1", 0, reason="leak", redacted_at=AT)
    assert SECRET not in _journal_text(tmp_path, "live1")


def test_journal_redaction_refuses_a_journal_another_process_holds(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from ahp_host_flow.engine import AgentRecord, JournalWriter, RunHeader\n"
        "h = RunHeader('other1', 'x.js', 's', False, None, 10, None)\n"
        "w = JournalWriter.create(Path(sys.argv[1]), h)\n"
        f"w.append_agent(AgentRecord(0, 'k', 'ok', {SECRET!r}))\n"
        "print('ready', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(JournalBusyError):
            redact_agent_record(tmp_path, "other1", 0, reason="leak", redacted_at=AT)
        assert SECRET in _journal_text(tmp_path, "other1")
    finally:
        assert child.stdin is not None
        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(timeout=20)
    redact_agent_record(tmp_path, "other1", 0, reason="leak", redacted_at=AT)
    assert SECRET not in _journal_text(tmp_path, "other1")
