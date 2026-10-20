"""The heartbeat: a run's main chat shows liveness while a step is in flight (M16)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_protocol.ext.flow import FLOW_META_KEY

from tests.flow_hosting import FlowFixture, boot_flow
from tests.hosting import run_turn, state

TOML = '[providers.scripted]\nload = "tests.limits_fakes:ScriptedProvider"\n'
LIMITS = "[limits]\nheartbeat = 0.05\n"

ONE_STEP = (
    "meta = {'name': 'beat', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('reason-then-text:0.4')\n"
)

# Runs for a while in the sandbox but never calls agent().
NO_STEP = (
    "meta = {'name': 'beat', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    total = 0\n"
    "    for i in range(3_000_000):\n"
    "        total += i\n"
    "    return total\n"
)


def activity_actions(fx: FlowFixture) -> list[dict[str, Any]]:
    return [
        frame["params"]["action"]
        for frame in fx.wire.frames
        if frame.get("method") == "action"
        and frame["params"].get("channel") == fx.chat
        and frame["params"]["action"].get("type") == "chat/activityChanged"
    ]


def write_workflow(base: Path, source: str) -> None:
    user = base / "user"
    user.mkdir()
    (user / "beat.py").write_text(source, encoding="utf-8")


async def test_heartbeat_run_ticks(tmp_path: Path) -> None:
    write_workflow(tmp_path, ONE_STEP)
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML + LIMITS)
    try:
        await run_turn(fx.wire, fx.chat, "t1", "/beat")
        beats = [a for a in activity_actions(fx) if "_meta" in a]
        # 0.4 s of step over a 0.05 s heartbeat is about 8 ticks; 3 tolerates a slow runner.
        assert len(beats) >= 3
        for tick, action in enumerate(beats, start=1):
            assert action["activity"] == "Running 1 step"
            assert action["_meta"][FLOW_META_KEY] == {"heartbeat": {"running": 1, "tick": tick}}
        assert activity_actions(fx)[-1].get("activity") is None
        assert state(fx.host, fx.chat).get("activity") is None
    finally:
        await fx.shut()


async def test_heartbeat_run_idle_when_no_steps(tmp_path: Path) -> None:
    write_workflow(tmp_path, NO_STEP)
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML + LIMITS)
    try:
        await run_turn(fx.wire, fx.chat, "t1", "/beat")
        assert activity_actions(fx) == []
    finally:
        await fx.shut()
