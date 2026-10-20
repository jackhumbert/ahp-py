"""Per-user quotas: the ledger file, the day roll-over and the Scheduler's two checks (M16)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ahp_host_flow import library
from ahp_host_flow.engine import (
    QUOTA_FILE,
    BudgetExhaustedError,
    QuotaLedger,
    QuotaLimits,
    RunOptions,
    load_journal,
)

from ..limits_fakes import UsageDispatcher  # noqa: TID252


def test_quota_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / QUOTA_FILE
    limits = QuotaLimits(tokens_per_day=100)
    first = QuotaLedger(path, limits, today=lambda: "2026-01-01")
    run = first.for_run("alice")
    assert run.check() is None
    run.charge(100, 0.0)
    again = QuotaLedger(path, limits, today=lambda: "2026-01-01")
    assert again.for_run("alice").check() == "quota tokens_per_day reached: 100 of 100"
    assert again.for_run("bob").check() is None


def test_quota_day_rolls_over(tmp_path: Path) -> None:
    path = tmp_path / QUOTA_FILE
    limits = QuotaLimits(tokens_per_day=100)
    QuotaLedger(path, limits, today=lambda: "2026-01-01").for_run("alice").charge(100, 0.0)
    tomorrow = QuotaLedger(path, limits, today=lambda: "2026-01-02")
    assert tomorrow.for_run("alice").check() is None


def test_quota_per_run(tmp_path: Path) -> None:
    ledger = QuotaLedger(None, QuotaLimits(tokens_per_run=50), today=lambda: "2026-01-01")
    one = ledger.for_run("alice")
    one.charge(50, 0.0)
    assert one.check() == "quota tokens_per_run reached: 50 of 50"
    # Live checks refuse only past the limit.
    assert one.check(live=True) is None
    assert one.check(extra_tokens=1, live=True) == "quota tokens_per_run reached: 51 of 50"
    # A new run of the same user starts at 0 for the run limit.
    assert ledger.for_run("alice").check() is None


def test_quota_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / QUOTA_FILE
    path.write_text("not json {{", encoding="utf-8")
    ledger = QuotaLedger(path, QuotaLimits(tokens_per_day=1), today=lambda: "2026-01-01")
    assert ledger.for_run("alice").check() is None
    ledger.for_run("alice").charge(1, 0.0)
    assert (
        QuotaLedger(path, QuotaLimits(tokens_per_day=1), today=lambda: "2026-01-01")
        .for_run("alice")
        .check()
    )


async def test_quota_scheduler_admission() -> None:
    ledger = QuotaLedger(None, QuotaLimits(tokens_per_run=100))
    logs: list[str] = []
    options = RunOptions(quota=ledger.for_run("alice-secret"))
    async with library.run(UsageDispatcher(), options, on_log=logs.append) as flow:
        assert await flow.agent("use:0:60:0:0") == "use:0:60:0:0"
        assert await flow.agent("use:0:60:0:0") == "use:0:60:0:0"
        with pytest.raises(BudgetExhaustedError, match="reached: 120 of 100"):
            await flow.agent("use:0:60:0:0")
    # Settling the second call (120 > 100) also runs the live check, which stays quiet: nothing
    # is in flight to stop. Only the admission refusal says the quota was reached.
    reached = [line for line in logs if "quota tokens_per_run reached: 120 of 100" in line]
    assert reached == ["quota tokens_per_run reached: 120 of 100; agent() #2 refused"]
    assert not any("stopping 0 agent() call(s)" in line for line in logs)
    assert not any("alice" in line for line in logs)


async def test_quota_scheduler_live(tmp_path: Path) -> None:
    ledger = QuotaLedger(None, QuotaLimits(tokens_per_run=100))
    dispatcher = UsageDispatcher()
    options = RunOptions(quota=ledger.for_run(None), state_dir=tmp_path / "state")
    async with library.run(dispatcher, options) as flow:
        (result,) = await asyncio.wait_for(
            asyncio.gather(flow.agent("burn:150"), return_exceptions=True), 10
        )
        assert flow.run_id is not None
        run_id = flow.run_id
    assert isinstance(result, BudgetExhaustedError)
    assert dispatcher.cancelled == [0]
    (record,) = load_journal(tmp_path / "state", run_id).agents
    assert (record.status, record.kind) == ("error", "budget")
