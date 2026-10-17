"""An opt-in smoke test: a real ACP agent returns a structured result.

Skipped unless ``AHP_ACP_LIVE_AGENT`` holds the agent's command line, for
example ``AHP_ACP_LIVE_AGENT="opencode acp"``. It talks to a live model, so it
never runs in the default suite.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

import pytest
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_protocol.ext.structured import OUTPUT_SCHEMA_META_KEY, STRUCTURED_OUTPUT_TOOL

from ahp_host_acp.provider import AcpProvider, AgentSpec

from .hosting import connect, found, open_session, shut, state
from .test_structured_acp import _calls

SCHEMA = {
    "type": "object",
    "properties": {"sum": {"type": "integer"}},
    "required": ["sum"],
}


@pytest.mark.skipif(not os.environ.get("AHP_ACP_LIVE_AGENT"), reason="AHP_ACP_LIVE_AGENT not set")
async def test_a_live_agent_returns_structured_output(tmp_path: Path) -> None:
    command = tuple(shlex.split(os.environ["AHP_ACP_LIVE_AGENT"]))
    host = Host(AcpProvider(tmp_path, AgentSpec(command)), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "acp:/live", "acp")
        await wire.dispatch(
            chat,
            {
                "type": "chat/turnStarted",
                "turnId": "t1",
                "startedAt": "1970-01-01T00:00:01.000Z",
                "message": {
                    "text": "What is 2 + 3? Return the sum.",
                    "origin": {"kind": "user"},
                    "_meta": {OUTPUT_SCHEMA_META_KEY: SCHEMA},
                },
            },
        )

        def finished() -> dict[str, Any] | None:
            turns = state(host, chat).get("turns", [])
            return next((t for t in turns if t.get("id") == "t1"), None)

        assert await wire.until(lambda: finished() is not None, timeout=180)
        turn = finished()
        assert turn is not None
        calls = _calls(turn)
        assert calls, found(turn, lambda m: m.get("kind") == "error")
        assert calls[-1]["toolName"] == STRUCTURED_OUTPUT_TOOL
        assert calls[-1]["structuredContent"] == {"sum": 5}
    finally:
        await shut(host, wire, serving)
