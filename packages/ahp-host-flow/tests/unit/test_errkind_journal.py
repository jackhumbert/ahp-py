"""Error kinds on journal error records and the Scheduler that writes them (M16)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ahp_host_flow.engine import (
    AgentCall,
    AgentRecord,
    AgentResult,
    DispatchError,
    JournalWriter,
    RunHeader,
    RunOptions,
    Scheduler,
    load_journal,
)


def _header() -> RunHeader:
    return RunHeader(
        run_id="r1",
        filename="s.js",
        source="return 1",
        has_args=False,
        args=None,
        max_agents=1000,
        budget_total=None,
        resumed_from=None,
    )


def _agent_lines(path: Path) -> list[dict[str, object]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["type"] == "agent"]


def test_errkind_journal_round_trip(tmp_path: Path) -> None:
    w = JournalWriter.create(tmp_path, _header())
    w.append_agent(AgentRecord(0, "k0", "error", None, 0, error="no", kind="refused"))
    w.append_agent(AgentRecord(1, "k1", "ok", 5, 1))
    w.close()
    lines = _agent_lines(w.path)
    assert lines[0]["kind"] == "refused"
    assert "kind" not in lines[1]
    agents = load_journal(tmp_path, "r1").agents
    assert agents[0].kind == "refused"
    assert agents[0].error == "no"
    assert agents[1].kind is None


class _RaisingDispatcher:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def run_agent(self, call: AgentCall) -> AgentResult:
        raise self._exc


async def _run_failing(tmp_path: Path, exc: Exception) -> tuple[list[str], AgentRecord]:
    logs: list[str] = []
    w = JournalWriter.create(tmp_path, _header())
    s = Scheduler(_RaisingDispatcher(exc), RunOptions(), on_log=logs.append, journal=w)
    assert await asyncio.wait_for(s.run(AgentCall(ordinal=0, prompt="p")), 10) is None
    w.close()
    return logs, load_journal(tmp_path, "r1").agents[0]


async def test_errkind_journal_scheduler_records_kind(tmp_path: Path) -> None:
    logs, rec = await _run_failing(tmp_path / "a", DispatchError("rate_limited", True, "slow down"))
    assert logs == ["agent() #0 failed: slow down"]
    assert (rec.status, rec.error, rec.kind) == ("error", "slow down", "rate_limited")
    logs, rec = await _run_failing(tmp_path / "b", RuntimeError("plain"))
    assert logs == ["agent() #0 failed: plain"]
    assert (rec.status, rec.error, rec.kind) == ("error", "plain", "failed")


def test_errkind_journal_old_reader_line(tmp_path: Path) -> None:
    w = JournalWriter.create(tmp_path, _header())
    w.close()
    line = {
        "type": "agent",
        "ordinal": 0,
        "key": "k",
        "status": "error",
        "value": None,
        "outputTokens": 0,
        "error": "old",
    }
    with w.path.open("ab") as f:
        f.write(json.dumps(line).encode() + b"\n")
    rec = load_journal(tmp_path, "r1").agents[0]
    assert (rec.error, rec.kind) == ("old", None)


class _ValueDispatcher:
    def __init__(self, value: object) -> None:
        self._value = value

    async def run_agent(self, call: AgentCall) -> AgentResult:
        return AgentResult(self._value)


async def test_errkind_journal_schema_and_not_json_are_failed(tmp_path: Path) -> None:
    w = JournalWriter.create(tmp_path, _header())
    schema = {"type": "object", "required": ["n"]}
    for n, (value, opts) in enumerate([({"m": 1}, {"schema": schema}), (float("nan"), {})]):
        s = Scheduler(_ValueDispatcher(value), RunOptions(), journal=w)
        s._next_ordinal = n
        call, cached = s.issue("x", opts)
        assert await s.run(call, cached) is None
        s.close()
    w.close()
    agents = load_journal(tmp_path, "r1").agents
    assert [(a.status, a.kind) for a in agents] == [("error", "failed"), ("error", "failed")]
    assert "result is not JSON" in (agents[1].error or "")
