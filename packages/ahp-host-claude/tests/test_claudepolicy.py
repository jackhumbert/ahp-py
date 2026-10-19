"""The host `ToolPolicy` in `_pre_tool_use` and `_can_use_tool` (security)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import AgentSessionContext, UserMessage
from ahp_host.provider.policy import ALLOW, Decision, ToolPolicy, ToolRequest
from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

from ahp_host_claude.__main__ import _parse_args
from ahp_host_claude.agent import parse_agent_options
from ahp_host_claude.config import ConfigError, load
from ahp_host_claude.permissions import (
    POLICY_GLOBS,
    POLICY_TOOLS,
    pre_tool_use_decision,
    tool_request,
)
from ahp_host_claude.provider import ClaudeSession
from tests.fakes import RecordingSink
from tests.test_client_tools import Harness as ToolsHarness
from tests.test_client_tools import _Call
from tests.test_client_tools import _context as _tools_context
from tests.test_client_tools import _result as _tools_result
from tests.test_file_edits import Harness, _edit, _result


def _session(tmp_path: Path, config: dict[str, Any] | None = None, **fields: Any) -> ClaudeSession:
    context = AgentSessionContext(
        session_uri="s", chat_uri="c", provider_id="claude", config=config or {}
    )
    return ClaudeSession(context, root=tmp_path, client_factory=lambda o: None, **fields)  # type: ignore[arg-type,return-value]


def _policy(**fields: Any) -> ToolPolicy:
    fields.setdefault("deny_commands", ("git push*",))
    fields.setdefault("deny_paths", ("**/.env",))
    return ToolPolicy(**fields)


def _decision(output: Any) -> Any:
    return output["hookSpecificOutput"]["permissionDecision"]


def test_claudepolicy_mapping_table_pinned(tmp_path: Path) -> None:
    assert dict(POLICY_TOOLS) == {
        "Bash": ("command", "command"),
        "Read": ("read", "file_path"),
        "NotebookRead": ("read", "notebook_path"),
        "LS": ("read", "path"),
        "Glob": ("read", "path"),
        "Grep": ("read", "path"),
        "Write": ("write", "file_path"),
        "Edit": ("write", "file_path"),
        "MultiEdit": ("write", "file_path"),
        "NotebookEdit": ("write", "notebook_path"),
    }
    kwargs: dict[str, Any] = {"cwd": tmp_path, "roots": (tmp_path,)}
    assert tool_request("mcp__client__ping", {"a": 1}, **kwargs).kind == "tool"
    assert tool_request("Glob", {}, **kwargs).input == {"paths": (".",)}
    assert tool_request("Glob", {"pattern": "*"}, **kwargs).input == {
        "paths": (".", os.path.join(".", "*"))
    }


@pytest.mark.parametrize("mode", ["acceptEdits", "auto", "bypassPermissions"])
async def test_claudepolicy_denied_push_in_every_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    config = {} if mode == "bypassPermissions" else {"permissionMode": mode}
    session = _session(tmp_path, config, policy=_policy())
    if mode == "bypassPermissions":
        monkeypatch.setattr(session, "_gate", lambda: "bypassPermissions")
    hook = await session._pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": "git push origin"}}, "t1", None
    )
    assert _decision(hook) == "deny"
    assert "'git push*'" in hook["hookSpecificOutput"]["permissionDecisionReason"]
    result = await session._can_use_tool(
        "Bash", {"command": "git push"}, ToolPermissionContext(tool_use_id="t1")
    )
    assert isinstance(result, PermissionResultDeny)
    assert "'git push*'" in result.message


async def test_claudepolicy_denied_path_and_client_tool(tmp_path: Path) -> None:
    def check(request: ToolRequest) -> Decision | None:
        if request.name == "mcp__client__ping":
            return Decision("deny", "no pings")
        return None

    session = _session(tmp_path, policy=_policy(check=check))
    hook = await session._pre_tool_use(
        {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / ".env")}}, "t1", None
    )
    assert _decision(hook) == "deny"
    # The client-tool allow would otherwise let this through.
    hook = await session._pre_tool_use(
        {"tool_name": "mcp__client__ping", "tool_input": {}}, "t2", None
    )
    assert _decision(hook) == "deny"
    # An ordinary read falls through to the existing decision.
    hook = await session._pre_tool_use(
        {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "README.md")}},
        "t3",
        None,
    )
    assert hook == pre_tool_use_decision("Read", session._gate())
    assert _decision(hook) == "allow"


async def test_claudepolicy_ask_forces_prompt(tmp_path: Path) -> None:
    session = _session(
        tmp_path, {"permissionMode": "acceptEdits"}, policy=_policy(ask_paths=("**/notes/*",))
    )
    hook = await session._pre_tool_use(
        {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "notes" / "a")}},
        "t1",
        None,
    )
    assert _decision(hook) == "ask"


async def test_claudepolicy_no_policy_unchanged(tmp_path: Path) -> None:
    session = _session(tmp_path, {"permissionMode": "acceptEdits"})
    hook = await session._pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": "git push"}}, "t1", None
    )
    assert hook == pre_tool_use_decision("Bash", "acceptEdits") == {}
    assert session._policy_decision("Bash", {"command": "ls"}) is None
    assert ALLOW.action == "allow"


def test_claudepolicy_config() -> None:
    options = parse_agent_options({"policy": {"deny_commands": ["git push*"]}})
    assert options.policy is not None
    assert options.policy.deny_commands == ("git push*",)
    with pytest.raises(ValueError, match="claude agent"):
        parse_agent_options({"policy": {"host_tools": True}})


def test_claudepolicy_glob_and_grep_paths(tmp_path: Path) -> None:
    assert dict(POLICY_GLOBS) == {"Glob": ("pattern",), "Grep": ("glob",)}
    kwargs: dict[str, Any] = {"cwd": tmp_path, "roots": (tmp_path,)}
    sub = str(tmp_path / "sub")
    assert tool_request("Glob", {"pattern": "**/.env", "path": sub}, **kwargs).input == {
        "paths": (sub, os.path.join(sub, "**/.env"))
    }
    assert tool_request("Grep", {"pattern": "x", "glob": "*.env"}, **kwargs).input == {
        "paths": (".", os.path.join(".", "*.env"))
    }
    policy = _policy()
    outside = (tmp_path.parent / "elsewhere").as_posix()
    for name, tool_input in (
        ("Glob", {"pattern": "**/.env"}),
        ("Glob", {"pattern": outside + "/*"}),
        ("Glob", {"pattern": "../*"}),
        ("Grep", {"pattern": "KEY", "glob": ".env"}),
        ("Grep", {"pattern": "KEY", "path": outside}),
        ("Grep", {"pattern": "KEY", "path": str(tmp_path), "glob": "../x/*"}),
    ):
        decision = policy.decide(tool_request(name, tool_input, **kwargs))
        assert decision.action == "deny", (name, tool_input)
    for name, tool_input in (
        ("Glob", {"pattern": "**/*.py"}),
        ("Grep", {"pattern": ".env", "glob": "*.py"}),
    ):
        assert policy.decide(tool_request(name, tool_input, **kwargs)).action == "allow"


async def test_claudepolicy_bash_words_are_paths(tmp_path: Path) -> None:
    session = _session(tmp_path, {"permissionMode": "acceptEdits"}, policy=_policy())
    for command in ("cat .env", "cp .env /tmp/x", "head -n 1 ./.env"):
        hook = await session._pre_tool_use(
            {"tool_name": "Bash", "tool_input": {"command": command}}, "t1", None
        )
        assert _decision(hook) == "deny", command
        assert "'**/.env'" in hook["hookSpecificOutput"]["permissionDecisionReason"]


async def test_claudepolicy_ask_keeps_the_edit_diff(tmp_path: Path) -> None:
    """An ask on a Write in Accept edits, approved: the call still gets its diff."""
    Harness(tmp_path)  # makes the folder
    target = tmp_path / "work" / "notes" / "a.txt"
    target.parent.mkdir()
    target.write_bytes(b"before\n")
    seen: list[Any] = []
    tool_input = {"file_path": str(target)}

    async def asked_then_approved(options: Any) -> None:
        hook = options.hooks["PreToolUse"][0].hooks[0]
        seen.append(await hook({"tool_name": "Write", "tool_input": tool_input}, "e1", None))
        target.write_bytes(b"after\n")  # the user approved; the call ran

    use, _, result = _edit("e1", "Write", tool_input, lambda: None)
    harness = Harness(tmp_path, [use, asked_then_approved, result, _result()])
    harness.provider.policy = _policy(ask_paths=("**/notes/*",))
    sink = await harness.run()
    assert _decision(seen[0]) == "ask"
    (change,) = sink.file_edits
    assert (change.before, change.after) == (b"before\n", b"after\n")


async def test_claudepolicy_ask_keeps_the_client_call_id(tmp_path: Path) -> None:
    harness = ToolsHarness(tmp_path)
    harness.provider.policy = ToolPolicy(
        check=lambda r: Decision("ask", "ask first") if r.name.startswith("mcp__client__") else None
    )
    session = await harness.provider.create_session(_tools_context())
    step = _Call(session, "toolu_1", {"node": "my-mac-mini"})
    harness.turns.append([step, _tools_result()])
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="start one"), sink)
    assert _decision(step.decision) == "ask"
    [call] = sink.client_calls
    assert call.call_id == "toolu_1"


def test_claudepolicy_settings_from_the_config_file(tmp_path: Path) -> None:
    config = tmp_path / "claude.toml"
    config.write_text(
        f"root = '{tmp_path}'\nenv_allow = ['PATH', 'LC_*']\n"
        "[policy]\ndeny_commands = ['git push*']\ndeny_paths = ['**/.env']\n"
    )
    settings = load(_parse_args(["--config", str(config)]))
    assert settings.env_allow == ("PATH", "LC_*")
    assert settings.policy is not None
    assert settings.policy.deny_commands == ("git push*",)
    assert settings.policy.deny_paths == ("**/.env",)
    assert load(_parse_args(["--config", str(_plain(tmp_path))])).policy is None
    for bad, match in (
        ("env_allow = 'PATH'", "env_allow"),
        ("policy = 'x'", "policy must be a table"),
        ("[policy]\nhost_tools = true", "host_tools"),
        ("[policy]\ndeny_paths = '**/.env'", "deny_paths"),
    ):
        config.write_text(f"root = '{tmp_path}'\n{bad}\n")
        with pytest.raises(ConfigError, match=match):
            load(_parse_args(["--config", str(config)]))


def _plain(tmp_path: Path) -> Path:
    config = tmp_path / "plain.toml"
    config.write_text(f"root = '{tmp_path}'\n")
    return config
