"""`retry_empty`: an empty reply is dispatched again; one ordinal, one journal record (M16)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ahp_host_flow.engine import load_journal
from ahp_host_flow.engine.journal import AgentRecord
from tests.flow_hosting import SESSION, boot_flow, final_json
from tests.hosting import found, run_turn, worker_chats
from tests.limits_fakes import ScriptedSession

TOML = '[providers.scripted]\nload = "tests.limits_fakes:ScriptedProvider"\n'


def workflow(prompt: str) -> str:
    return (
        "meta = {'name': 'retry', 'description': 'd'}\n"
        "\n"
        "async def main(args):\n"
        f"    return await agent({prompt!r}, label='Retry')\n"
    )


async def _run(
    tmp_path: Path, prompt: str, retries: int
) -> tuple[Mapping[str, Any], Sequence[AgentRecord], set[str], int]:
    ScriptedSession.reset()
    user = tmp_path / "user"
    user.mkdir()
    (user / "retry.py").write_text(workflow(prompt), encoding="utf-8")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), f"{TOML}retry_empty = {retries}\n")
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/retry")
        chats = len(worker_chats(fx.host, SESSION))
    finally:
        await fx.shut()
    calls = {
        str(m["toolCallId"])
        for m in found(turn, lambda m: m.get("toolName") == "agent" and "toolCallId" in m)
    }
    (run,) = (tmp_path / "state" / "runs").iterdir()
    return turn, load_journal(tmp_path / "state", run.name).agents, calls, chats


async def test_retryempty_then_text(tmp_path: Path) -> None:
    turn, agents, calls, chats = await _run(tmp_path, "empty-then-text", 1)
    assert final_json(turn) == "second"
    (record,) = agents
    assert (record.ordinal, record.status, record.value) == (0, "ok", "second")
    assert record.output_tokens == 6
    assert chats == 2
    assert calls == {"agent-0", "agent-0-a1"}


async def test_retryempty_default_unchanged(tmp_path: Path) -> None:
    turn, agents, calls, chats = await _run(tmp_path, "empty-then-text", 0)
    assert final_json(turn) == ""
    (record,) = agents
    assert record.status == "ok"
    assert chats == 1
    assert calls == {"agent-0"}


async def test_retryempty_exhausted(tmp_path: Path) -> None:
    turn, agents, _, chats = await _run(tmp_path, "always-empty", 1)
    assert final_json(turn) is None
    (record,) = agents
    assert (record.status, record.kind) == ("error", "empty")
    assert "returned an empty reply 2 times" in (record.error or "")
    # Both attempts' usage is counted, though neither answered.
    assert record.output_tokens == 6
    assert chats == 2
