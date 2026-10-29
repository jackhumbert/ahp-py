"""The journal header's optional `principal` (a client's continue is checked against it)."""

from __future__ import annotations

import json
from pathlib import Path

from ahp_host_flow.engine import JournalWriter, RunHeader, load_journal, run_dir


def _header(principal: str | None) -> RunHeader:
    return RunHeader("r1", "a.js", "return 1", False, None, 10, None, principal=principal)


def test_principal_is_written_only_when_set_and_read_back(tmp_path: Path) -> None:
    JournalWriter.create(tmp_path, _header("alice@example.com")).close()
    assert load_journal(tmp_path, "r1").header.principal == "alice@example.com"
    (tmp_path / "other").mkdir()
    JournalWriter.create(tmp_path / "other", _header(None)).close()
    path = run_dir(tmp_path / "other", "r1") / "journal.jsonl"
    assert "principal" not in json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert load_journal(tmp_path / "other", "r1").header.principal is None
