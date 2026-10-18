"""Custom verb journal records: round trip, masking, keys and replay (plan M12)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import (
    AgentRecord,
    JournalError,
    JournalRun,
    JournalWriter,
    Redactor,
    RunHeader,
    RunOptions,
    VerbRecord,
    VerbReplay,
    canonical_json,
    load_journal,
    redact_agent_record,
    verb_call_id,
    verb_key,
)


def _header(run_id: str = "r1") -> RunHeader:
    return RunHeader(run_id, "w.js", "return 1", False, None, 10, None)


def _journal_path(state: Path, run_id: str) -> Path:
    return state / "runs" / run_id / "journal.jsonl"


def _append(state: Path, run_id: str, record: dict[str, Any]) -> None:
    with _journal_path(state, run_id).open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record) + "\n")


def _verb_line(**fields: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "type": "verb",
        "ordinal": 0,
        "key": "k",
        "name": "tool",
        "callId": "verb-k-0",
        "status": "open",
        "value": None,
        "error": None,
    }
    line.update(fields)
    return line


def test_customverb_journal_round_trip(tmp_path: Path) -> None:
    key0, key1, key2 = (verb_key(n, [i]) for i, n in enumerate(("tool", "render", "approve")))
    writer = JournalWriter.create(tmp_path, _header())
    writer.append_verb(VerbRecord(0, key0, "tool", "verb-a-0", "open"))
    writer.append_verb(VerbRecord(0, key0, "tool", "verb-a-0", "done", {"x": 1}))
    writer.append_verb(VerbRecord(1, key1, "render", "verb-b-0", "error", None, "boom"))
    writer.append_verb(VerbRecord(2, key2, "approve", "verb-c-0", "open"))
    writer.close()
    with _journal_path(tmp_path, "r1").open("a", encoding="utf-8", newline="\n") as f:
        f.write('{"type":"future"}\n')
    loaded = load_journal(tmp_path, "r1")
    assert loaded.verbs == (
        VerbRecord(0, key0, "tool", "verb-a-0", "done", {"x": 1}),
        VerbRecord(1, key1, "render", "verb-b-0", "error", None, "boom"),
        VerbRecord(2, key2, "approve", "verb-c-0", "open"),
    )
    assert loaded.open_verbs == (VerbRecord(2, key2, "approve", "verb-c-0", "open"),)


def test_customverb_journal_finished_wins(tmp_path: Path) -> None:
    writer = JournalWriter.create(tmp_path, _header())
    writer.append_verb(VerbRecord(0, "k", "tool", "verb-k-0", "done", 7))
    writer.append_verb(VerbRecord(1, "k", "tool", "verb-k-1", "error", None, "bad"))
    writer.close()
    _append(tmp_path, "r1", _verb_line())
    _append(tmp_path, "r1", _verb_line(ordinal=1, callId="verb-k-1"))
    loaded = load_journal(tmp_path, "r1")
    assert loaded.verbs == (
        VerbRecord(0, "k", "tool", "verb-k-0", "done", 7),
        VerbRecord(1, "k", "tool", "verb-k-1", "error", None, "bad"),
    )
    assert loaded.open_verbs == ()


@pytest.mark.parametrize(
    "fields",
    [
        {"ordinal": -1},
        {"ordinal": "0"},
        {"ordinal": True},
        {"key": 1},
        {"name": None},
        {"callId": 5},
        {"status": "maybe"},
        {"status": None},
        {"error": 3},
    ],
)
def test_customverb_journal_bad_record(tmp_path: Path, fields: dict[str, Any]) -> None:
    JournalWriter.create(tmp_path, _header()).close()
    _append(tmp_path, "r1", _verb_line(**fields))
    with pytest.raises(JournalError, match="bad verb record"):
        load_journal(tmp_path, "r1")


def test_customverb_journal_masked(tmp_path: Path) -> None:
    writer = JournalWriter.create(tmp_path, _header(), mask=Redactor().mask_value)
    writer.append_verb(VerbRecord(0, "k", "tool", "verb-k-0", "done", "token=abcdefgh12345678"))
    writer.append_verb(
        VerbRecord(1, "k", "tool", "verb-k-1", "error", None, "token=abcdefgh12345678")
    )
    writer.close()
    text = _journal_path(tmp_path, "r1").read_text(encoding="utf-8")
    assert "abcdefgh12345678" not in text
    loaded = load_journal(tmp_path, "r1")
    assert loaded.verbs == (
        VerbRecord(0, "k", "tool", "verb-k-0", "done", "token=[redacted]"),
        VerbRecord(1, "k", "tool", "verb-k-1", "error", None, "token=[redacted]"),
    )


def test_customverb_journal_key_and_call_id() -> None:
    expected = hashlib.sha256(
        canonical_json({"args": ["a", 1], "name": "tool"}).encode("utf-8")
    ).hexdigest()
    key = verb_key("tool", ["a", 1])
    assert key == expected
    assert verb_key("tool", ("a", 1)) == key
    assert verb_key("tool", [1, "a"]) != key
    assert verb_key("render", ["a", 1]) != key
    call_id = verb_call_id(key, 3)
    assert call_id == f"verb-{key[:16]}-3"
    assert len(call_id) == 5 + 16 + 1 + 1
    assert verb_call_id(key, 12) == f"verb-{key[:16]}-12"
    assert len(verb_call_id(key, 12)) == 5 + 16 + 1 + 2


def test_customverb_journal_replay() -> None:
    done = VerbRecord(4, "k1", "tool", "verb-k1-0", "done", "v")
    failed = VerbRecord(5, "k2", "tool", "verb-k2-0", "error", None, "boom")
    cut = VerbRecord(6, "k3", "tool", "verb-k3-0", "open")
    replay = VerbReplay([cut, failed, done])
    assert replay.lookup("verb-k1-0", "other", 0) is None
    assert replay.lookup("verb-nope-0", "k1", 0) is None
    assert replay.lookup("verb-k3-0", "k3", 0) is None
    assert replay.lookup("verb-k1-0", "k1", 0) == VerbRecord(
        0, "k1", "tool", "verb-k1-0", "done", "v"
    )
    assert replay.lookup("verb-k1-0", "k1", 1) is None
    assert replay.lookup("verb-k2-0", "k2", 2) == VerbRecord(
        2, "k2", "tool", "verb-k2-0", "error", None, "boom"
    )
    assert replay.served == [0, 2]
    assert replay.was_open("verb-k3-0")
    assert not replay.was_open("verb-k1-0")
    assert not replay.was_open("verb-k2-0")
    assert not replay.was_open("verb-nope-0")


def test_customverb_journal_resume_opens_replay(tmp_path: Path) -> None:
    first = JournalRun.open(
        RunOptions(state_dir=tmp_path, run_id="first"),
        source="return 1",
        filename="w.js",
        has_args=False,
        args=None,
    )
    assert first.verb_replay is None
    first.writer.append_verb(VerbRecord(0, "k", "tool", "verb-k-0", "done", 1))
    first.writer.append_verb(VerbRecord(1, "j", "tool", "verb-j-0", "open"))
    first.close()
    second = JournalRun.open(
        RunOptions(state_dir=tmp_path, run_id="second", resume_from="first"),
        source=None,
        filename="",
        has_args=False,
        args=None,
    )
    try:
        assert second.verb_replay is not None
        assert second.verb_replay.was_open("verb-j-0")
        assert second.verb_replay.lookup("verb-k-0", "k", 3) == VerbRecord(
            3, "k", "tool", "verb-k-0", "done", 1
        )
    finally:
        second.close()


def test_customverb_journal_redact_keeps_verb_line(tmp_path: Path) -> None:
    writer = JournalWriter.create(tmp_path, _header())
    writer.append_agent(AgentRecord(0, "k", "ok", "secret value"))
    writer.append_verb(VerbRecord(0, "k", "tool", "verb-k-0", "open"))
    writer.append_verb(VerbRecord(0, "k", "tool", "verb-k-0", "done", "kept"))
    writer.close()
    path = _journal_path(tmp_path, "r1")

    def verb_lines() -> list[bytes]:
        return [ln for ln in path.read_bytes().split(b"\n") if b'"type":"verb"' in ln]

    before = verb_lines()
    assert len(before) == 2
    redact_agent_record(tmp_path, "r1", 0, reason="test", redacted_at="2026-01-01T00:00:00Z")
    assert verb_lines() == before
