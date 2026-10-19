"""Confirmation hints on a tool confirmation (security-relevant)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import AgentSessionContext
from ahp_host.provider.policy import ToolPolicy
from claude_agent_sdk import ToolPermissionContext

from ahp_host_claude.permissions import POLICY_TOOLS, read_only_hint
from ahp_host_claude.provider import ClaudeSession, _Turn
from tests.fakes import RecordingSink


def _session(tmp_path: Path, **fields: Any) -> ClaudeSession:
    context = AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude", config={})
    return ClaudeSession(context, root=tmp_path, client_factory=lambda o: None, **fields)  # type: ignore[arg-type,return-value]


def test_claudehints_table_pinned() -> None:
    for name in ("Read", "NotebookRead", "LS", "Glob", "Grep"):
        assert read_only_hint(name) is True
    for name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
        assert read_only_hint(name) is False
    assert read_only_hint("mcp__x__y") is None
    assert set(POLICY_TOOLS) == {
        "Read",
        "NotebookRead",
        "LS",
        "Glob",
        "Grep",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Bash",
    }


@pytest.mark.parametrize("asked", [False, True])
async def test_claudehints_on_the_confirmation(tmp_path: Path, asked: bool) -> None:
    policy = ToolPolicy(ask_paths=("**/notes/*",)) if asked else None
    session = _session(tmp_path, policy=policy)
    sink = RecordingSink()
    session._begin(_Turn(sink=sink))
    if asked:
        name, tool_input = "Write", {"file_path": str(tmp_path / "notes" / "a"), "content": "x"}
    else:
        name, tool_input = "Read", {"file_path": str(tmp_path / "a")}
    await session._can_use_tool(name, tool_input, ToolPermissionContext(tool_use_id="t1"))
    confirmation = sink.confirmations[0]
    assert confirmation.policy_asked is asked
    assert confirmation.read_only is (not asked)
