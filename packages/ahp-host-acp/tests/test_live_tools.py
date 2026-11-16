"""An opt-in smoke test: a real ACP agent reads a file, writes one and runs a command.

Skipped unless ``AHP_ACP_LIVE_AGENT`` holds the agent's command line, for
example ``AHP_ACP_LIVE_AGENT="qwen --acp"``. ``AHP_ACP_LIVE_CONFIG`` sets ACP
session config options, as ``id=value`` pairs separated by spaces (``mode=auto``
for Qwen Code). It talks to a live model, so it never runs in the default suite.

Every approval the agent asks for is granted once and counted, so the test also
shows how often an agent in an automatic mode still stops to ask.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any

import pytest
from ahp_host import Host, LoopbackSingleUserPolicy

from ahp_host_acp.provider import AcpProvider, AgentSpec

from .hosting import Wire, connect, found, open_session, shut, state, text_of

TURN = "t1"
PROMPT = (
    "Read notes.txt in the current folder. Write its words in upper case to result.txt. "
    'Then run the shell command `python -c "print(6 * 7)"` and tell me what it printed.'
)


def _config() -> dict[str, str | bool]:
    options: dict[str, str | bool] = {}
    for pair in shlex.split(os.environ.get("AHP_ACP_LIVE_CONFIG", "")):
        key, value = pair.split("=", 1)
        options[key] = value
    return options


def _approve_everything(wire: Wire, chat: str, asked: list[str]) -> asyncio.Task[None]:
    """Grant each pending confirmation once, recording what was asked."""

    async def loop() -> None:
        answered: set[str] = set()
        while True:
            pending = found(
                state(wire.host, chat), lambda m: m.get("status") == "pending-confirmation"
            )
            for call in pending:
                call_id = str(call.get("toolCallId"))
                if call_id in answered:
                    continue
                answered.add(call_id)
                asked.append(str(call.get("invocationMessage") or call.get("toolName")))
                options = call.get("options") or []
                once = next((o for o in options if o.get("kind") == "approve"), None)
                action: dict[str, Any] = {
                    "type": "chat/toolCallConfirmed",
                    "turnId": TURN,
                    "toolCallId": call_id,
                    "approved": True,
                }
                if once is not None:
                    action["selectedOptionId"] = once.get("id")
                await wire.dispatch(chat, action)
            await asyncio.sleep(0.05)

    return asyncio.create_task(loop())


@pytest.mark.skipif(not os.environ.get("AHP_ACP_LIVE_AGENT"), reason="AHP_ACP_LIVE_AGENT not set")
async def test_a_live_agent_reads_writes_and_runs(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("quiet hangar lights\n", encoding="utf-8")
    command = tuple(shlex.split(os.environ["AHP_ACP_LIVE_AGENT"], posix=os.name != "nt"))
    spec = AgentSpec(command, config_options=_config())
    host = Host(AcpProvider(tmp_path, spec), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    asked: list[str] = []
    approver: asyncio.Task[None] | None = None
    try:
        chat = await open_session(wire, "acp:/live-tools", "acp")
        approver = _approve_everything(wire, chat, asked)
        await wire.dispatch(
            chat,
            {
                "type": "chat/turnStarted",
                "turnId": TURN,
                "startedAt": "1970-01-01T00:00:01.000Z",
                "message": {"text": PROMPT, "origin": {"kind": "user"}},
            },
        )

        def finished() -> dict[str, Any] | None:
            turns = state(host, chat).get("turns", [])
            return next((t for t in turns if t.get("id") == TURN), None)

        assert await wire.until(lambda: finished() is not None, timeout=900)
        turn = finished()
        assert turn is not None
        calls = found(turn, lambda m: "toolCallId" in m and "status" in m)
        print("\ntool calls:", [(c.get("toolName"), c.get("status")) for c in calls])
        print("approvals asked:", asked)
        print("reply:", text_of(turn)[-500:])
        result = tmp_path / "result.txt"
        assert result.exists(), found(turn, lambda m: m.get("kind") == "error")
        assert result.read_text(encoding="utf-8").strip() == "QUIET HANGAR LIGHTS"
        assert "42" in text_of(turn)
    finally:
        if approver is not None:
            approver.cancel()
        await shut(host, wire, serving)
