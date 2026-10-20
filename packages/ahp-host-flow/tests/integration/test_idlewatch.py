"""The idle watchdog on in-process providers: reasoning is a heartbeat, approvals pause it."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ahp_host_flow.config import parse_config
from ahp_host_flow.engine import load_journal
from ahp_host_flow.engine.journal import AgentRecord
from tests.flow_hosting import SESSION, boot_flow, final_json
from tests.hosting import found, run_turn, start_turn, state, wait_turn, worker_chats
from tests.limits_fakes import ScriptedProvider, ScriptedSession

TOML = '[providers.scripted]\nload = "tests.limits_fakes:ScriptedProvider"\nidle_timeout = 0.3\n'


def workflow(prompt: str) -> str:
    return (
        "meta = {'name': 'idle', 'description': 'd'}\n"
        "\n"
        "async def main(args):\n"
        f"    return await agent({prompt!r}, label='Idle')\n"
    )


def journal(tmp_path: Path) -> Sequence[AgentRecord]:
    (run,) = (tmp_path / "state" / "runs").iterdir()
    return load_journal(tmp_path / "state", run.name).agents


def write_workflow(tmp_path: Path, prompt: str) -> None:
    user = tmp_path / "user"
    user.mkdir()
    (user / "idle.py").write_text(workflow(prompt), encoding="utf-8")


async def test_idlewatch_reasoning_is_heartbeat(tmp_path: Path) -> None:
    ScriptedSession.reset()
    write_workflow(tmp_path, "reason-then-text:1.0")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        turn = await asyncio.wait_for(run_turn(fx.wire, fx.chat, "t1", "/idle"), timeout=10)
    finally:
        await fx.shut()
    assert final_json(turn) == "done"
    (record,) = journal(tmp_path)
    assert record.status == "ok"


async def test_idlewatch_silent_times_out(tmp_path: Path) -> None:
    ScriptedSession.reset()
    write_workflow(tmp_path, "silent")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    began = time.monotonic()
    try:
        turn = await asyncio.wait_for(run_turn(fx.wire, fx.chat, "t1", "/idle"), timeout=5)
    finally:
        await fx.shut()
    assert time.monotonic() - began < 5
    assert final_json(turn) is None
    (record,) = journal(tmp_path)
    assert (record.status, record.kind) == ("error", "timeout")
    assert "no activity for 0.3s (idle_timeout)" in (record.error or "")
    (session,) = ScriptedProvider.instances[0].sessions
    assert session.cancelled == ["idle timeout"]
    assert not found(turn, lambda m: m.get("status") == "running")


async def test_idlewatch_approval_pauses(tmp_path: Path) -> None:
    ScriptedSession.reset()
    write_workflow(tmp_path, "ask-then-text:1.0")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/idle")
        assert await fx.wire.until(lambda: len(worker_chats(fx.host, SESSION)) == 1)
        worker = str(worker_chats(fx.host, SESSION)[0]["resource"])
        await fx.wire.request("subscribe", {"channel": worker})

        def pending() -> list[Any]:
            active = state(fx.host, worker).get("activeTurn") or {}
            return list(found(active, lambda m: m.get("status") == "pending-confirmation"))

        assert await fx.wire.until(lambda: bool(pending()))
        # Three times the idle timeout with nobody answering: the watchdog must not fire.
        await asyncio.sleep(1.0)
        assert state(fx.host, worker).get("activeTurn") is not None
        await fx.wire.dispatch(
            worker,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": state(fx.host, worker)["activeTurn"]["id"],
                "toolCallId": pending()[0]["toolCallId"],
                "approved": True,
                "confirmed": "user-action",
            },
        )
        turn = await wait_turn(fx.wire, fx.chat, "t1")
    finally:
        await fx.shut()
    assert final_json(turn) == "done"
    (record,) = journal(tmp_path)
    assert record.status == "ok"


def test_idlewatch_config_all_kinds(tmp_path: Path) -> None:
    data = {
        "providers": {
            "mine": {"load": "tests.limits_fakes:ScriptedProvider", "idle_timeout": 30},
            "fleet": {
                "via": "fleet",
                "node": "n1",
                "agent": "claude",
                "idle_timeout": 45.5,
                "retry_empty": 2,
            },
            "far": {"url": "wss://far.example.com", "agent": "claude", "idle_timeout": 60},
        },
        "routing": {"default": "mine"},
    }
    config = parse_config(data, base=tmp_path)
    assert config.providers["mine"].idle_timeout == 30.0
    assert config.providers["mine"].retry_empty == 0
    assert config.providers["fleet"].idle_timeout == 45.5
    assert config.providers["fleet"].retry_empty == 2
    assert config.providers["far"].idle_timeout == 60.0
