"""Per-call timeouts: `opts.x.timeout` and `ProviderConfig.call_timeout` (M16)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ahp_host_flow.engine import load_journal
from ahp_host_flow.engine.journal import AgentRecord
from tests.flow_fakes import HangingProvider, reset_fakes
from tests.flow_hosting import boot_flow, final_json
from tests.hosting import found, run_turn

TOML = '[providers.hang]\nload = "tests.flow_fakes:HangingProvider"\n'
TIMEOUT_TOML = TOML + "call_timeout = 0.2\n"


def workflow(options: str) -> str:
    return (
        "meta = {'name': 'slow', 'description': 'd'}\n"
        "\n"
        "async def main(args):\n"
        f"    return await agent('hang'{options})\n"
    )


async def _run(
    tmp_path: Path, toml: str, options: str
) -> tuple[dict[str, Any], Sequence[AgentRecord]]:
    reset_fakes()
    user = tmp_path / "user"
    user.mkdir()
    (user / "slow.py").write_text(workflow(options), encoding="utf-8")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), toml)
    try:
        turn = await asyncio.wait_for(run_turn(fx.wire, fx.chat, "t1", "/slow"), timeout=5)
    finally:
        await fx.shut()
    (run,) = (tmp_path / "state" / "runs").iterdir()
    return turn, load_journal(tmp_path / "state", run.name).agents


def check_timed_out(turn: dict[str, Any], agents: Sequence[AgentRecord]) -> None:
    assert final_json(turn) is None
    (record,) = agents
    assert (record.status, record.kind) == ("error", "timeout")
    assert "took longer than 0.2s" in (record.error or "")
    (session,) = HangingProvider.instances[0].sessions
    assert session.cancelled
    (done,) = found(turn, lambda m: m.get("pastTenseMessage") == "agent() failed")
    assert done["success"] is False
    assert not found(turn, lambda m: m.get("status") == "running")


async def test_calltimeout_opts_x(tmp_path: Path) -> None:
    turn, agents = await _run(tmp_path, TOML, ", x={'timeout': 0.2}")
    check_timed_out(turn, agents)
    assert "(opts.x.timeout)" in (agents[0].error or "")


async def test_calltimeout_provider_default(tmp_path: Path) -> None:
    turn, agents = await _run(tmp_path, TIMEOUT_TOML, "")
    check_timed_out(turn, agents)
    assert "(call_timeout)" in (agents[0].error or "")


async def test_calltimeout_bad_value(tmp_path: Path) -> None:
    turn, agents = await _run(tmp_path, TOML, ", x={'timeout': 'soon'}")
    assert final_json(turn) is None
    (record,) = agents
    assert record.status == "error"
    assert "opts.x.timeout" in (record.error or "")
    assert not HangingProvider.instances or not HangingProvider.instances[0].sessions
