"""Approval modes: ask (default), acceptEdits, and Claude Code's auto mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agent_host_server.provider.base import (
    AgentSessionContext,
    ConfigRequest,
    ConfiguresSessions,
    UserMessage,
)
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from agent_host_server_claude.permissions import pre_tool_use_decision
from agent_host_server_claude.provider import ClaudeProvider, ClaudeSession, _Turn
from tests.fakes import FakeClient, RecordingSink


def _context(config: dict[str, Any] | None = None) -> AgentSessionContext:
    return AgentSessionContext(
        session_uri="s", chat_uri="c", provider_id="claude", config=config or {}
    )


def _session(tmp_path: Path, config: dict[str, Any] | None = None) -> ClaudeSession:
    return ClaudeSession(_context(config), root=tmp_path, client_factory=lambda o: None)  # type: ignore[arg-type,return-value]


async def test_the_provider_offers_an_approvals_setting(tmp_path: Path) -> None:
    from agent_host_server_claude.sessions import ClaudeCodeSessions

    provider = ClaudeProvider(tmp_path, sessions=ClaudeCodeSessions(tmp_path, list_fn=lambda: []))
    assert isinstance(provider, ConfiguresSessions)
    resolution = await provider.resolve_config(ConfigRequest())
    prop = resolution.properties["permissionMode"]
    assert prop["enum"] == ["default", "acceptEdits", "auto", "plan"]
    assert prop["enumLabels"] == ["Ask", "Accept edits", "Auto", "Plan"]
    assert prop["default"] == "default"
    assert prop["sessionMutable"] is True  # switchable mid-session (config_changed)
    assert resolution.values["permissionMode"] == "default"
    chosen = await provider.resolve_config(ConfigRequest(values={"permissionMode": "auto"}))
    assert chosen.values["permissionMode"] == "auto"


@pytest.mark.parametrize(
    ("chosen", "mode", "permission_mode"),
    [
        (None, "default", "default"),
        ("default", "default", "default"),
        ("ask", "default", "default"),  # the setting's name before it took Claude Code's
        ("acceptEdits", "acceptEdits", "acceptEdits"),
        ("auto", "auto", "auto"),
        ("bypassPermissions", "default", "default"),  # never reachable from a client
        ("plan", "plan", "plan"),
        (7, "default", "default"),
    ],
)
def test_the_chosen_mode_sets_claude_codes_permission_mode(
    tmp_path: Path, chosen: Any, mode: str, permission_mode: str
) -> None:
    session = _session(tmp_path, {} if chosen is None else {"permissionMode": chosen})
    assert session.approvals == mode
    assert session._options().permission_mode == permission_mode


@pytest.mark.parametrize("mode", ["acceptEdits", "auto"])
def test_looser_modes_leave_changing_tools_to_claude_code(mode: str) -> None:
    assert pre_tool_use_decision("Bash", mode) == {}
    assert pre_tool_use_decision("Edit", mode) == {}
    # Reads stay allowed outright in every mode.
    assert pre_tool_use_decision("Read", mode)["hookSpecificOutput"]["permissionDecision"] == (
        "allow"
    )


def test_ask_mode_still_forces_the_prompt() -> None:
    decision = pre_tool_use_decision("Bash", "default")
    assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


async def test_a_resumed_session_keeps_its_mode(tmp_path: Path) -> None:
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        clients.append(FakeClient(options, [[result]]))
        return clients[-1]

    provider = ClaudeProvider(tmp_path, client_factory=factory)
    session = await provider.create_session(_context({"permissionMode": "auto"}))
    await session.send_user_message(UserMessage(text="x"), RecordingSink())
    state = await provider.resume_state_of(session)
    assert state == {"claudeSessionId": "abc", "permissionMode": "auto", "remoteControl": False}

    resumed = await provider.resume_session(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude", resume_state=state)
    )
    assert resumed.approvals == "auto"
    old = await provider.resume_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            resume_state={"claudeSessionId": "a"},
        )
    )
    assert old.approvals == "default"
    legacy = await provider.resume_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            resume_state={"claudeSessionId": "a", "approvals": "auto"},
        )
    )
    assert legacy.approvals == "auto"


async def test_plan_mode_shows_the_plan_and_drops_to_ask_once_approved(tmp_path: Path) -> None:
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    from agent_host_server_claude.permissions import APPROVALS_PROPERTY

    assert "plan" in APPROVALS_PROPERTY["enum"]
    session = _session(tmp_path, {"permissionMode": "plan"})
    assert session._options().permission_mode == "plan"
    # In plan mode the hook stays out of the way; Claude Code blocks edits itself.
    assert pre_tool_use_decision("Write", "plan") == {}

    sink = RecordingSink(approve=True)
    session._begin(_Turn(sink=sink))
    plan_input = {"plan": "1. Add x.txt\n2. Done"}
    result = await session._can_use_tool(
        "ExitPlanMode", plan_input, ToolPermissionContext(tool_use_id="p1")
    )
    assert isinstance(result, PermissionResultAllow)
    # The plan is in the chat before the approval prompt.
    kinds = [e[0] for e in sink.events]
    assert kinds.index("text") < kinds.index("confirm")
    assert ("text", "\n\n1. Add x.txt\n2. Done\n") in sink.events
    assert sink.confirmations[0].display_name == "Start on the plan"
    # And the work that follows is approved call by call.
    assert session.approvals == "default"
    assert (
        pre_tool_use_decision("Write", session.approvals)["hookSpecificOutput"][
            "permissionDecision"
        ]
        == "ask"
    )


async def test_a_rejected_plan_stays_in_plan_mode(tmp_path: Path) -> None:
    from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

    session = _session(tmp_path, {"permissionMode": "plan"})
    session._begin(_Turn(sink=RecordingSink(approve=False)))
    result = await session._can_use_tool(
        "ExitPlanMode", {"plan": "p"}, ToolPermissionContext(tool_use_id="p2")
    )
    assert isinstance(result, PermissionResultDeny)
    assert session.approvals == "plan"


async def test_the_mode_can_change_mid_session(tmp_path: Path) -> None:
    """Switching the mode moves both the PreToolUse gate and the running
    Claude client, and the next resume keeps the new mode."""
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        clients.append(FakeClient(options, [[result]]))
        return clients[-1]

    provider = ClaudeProvider(tmp_path, client_factory=factory)
    resolution = await provider.resolve_config(ConfigRequest())
    assert resolution.properties["permissionMode"]["sessionMutable"] is True

    session = await provider.create_session(_context({"permissionMode": "default"}))
    await session.send_user_message(UserMessage(text="x"), RecordingSink())
    assert (
        pre_tool_use_decision("Bash", session.approvals)["hookSpecificOutput"]["permissionDecision"]
        == "ask"
    )

    await session.config_changed({"permissionMode": "auto"})
    assert session.approvals == "auto"
    assert clients[0].permission_modes == ["auto"]
    saved = await provider.resume_state_of(session)
    assert saved is not None
    assert saved["permissionMode"] == "auto"

    # Back to Ask: the gate asks for everything again.
    await session.config_changed({"permissionMode": "default"})
    assert clients[0].permission_modes == ["auto", "default"]
    assert (
        pre_tool_use_decision("Bash", session.approvals)["hookSpecificOutput"]["permissionDecision"]
        == "ask"
    )


async def test_an_unknown_mode_falls_back_to_ask(tmp_path: Path) -> None:
    session = _session(tmp_path, {"permissionMode": "auto"})
    await session.config_changed({"permissionMode": "bypassPermissions"})
    assert session.approvals == "default"


async def test_a_change_before_the_first_turn_applies_when_the_client_starts(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    await session.config_changed({"permissionMode": "acceptEdits"})
    assert session.approvals == "acceptEdits"


async def test_resuming_prefers_the_sessions_current_config(tmp_path: Path) -> None:
    provider = ClaudeProvider(tmp_path, client_factory=lambda o: None)  # type: ignore[arg-type,return-value]
    resumed = await provider.resume_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            config={"permissionMode": "plan"},
            resume_state={"claudeSessionId": "a", "permissionMode": "auto"},
        )
    )
    assert resumed.approvals == "plan"
