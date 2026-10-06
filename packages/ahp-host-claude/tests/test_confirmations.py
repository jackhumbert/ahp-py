"""Approval prompts: the choices offered, a preview of the edit, why the user said no."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import (
    AgentSessionContext,
    ConfirmationOption,
    ToolConfirmationOutcome,
    UserMessage,
)
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    ResultMessage,
    ToolPermissionContext,
    ToolUseBlock,
)
from claude_agent_sdk.types import PermissionRuleValue

from ahp_host_claude.edits import preview
from ahp_host_claude.permissions import choices, denial_message
from ahp_host_claude.provider import ClaudeProvider
from ahp_host_claude.roots import Roots
from tests.fakes import FakeClient, FakePublisher, RecordingSink, Step

BASH_RULE = PermissionUpdate(
    type="addRules",
    rules=[PermissionRuleValue(tool_name="Bash", rule_content="npm test:*")],
    behavior="allow",
    destination="localSettings",
)
ACCEPT_EDITS = PermissionUpdate(type="setMode", mode="acceptEdits", destination="session")


class TestChoices:
    def test_an_allow_rule_is_offered_for_this_session_only(self) -> None:
        offered = choices([BASH_RULE], "acceptEdits")
        assert [o.id for o in offered.options] == ["allow", "suggestion:0", "deny"]
        assert offered.options[1].label == "Allow `Bash(npm test:*)` for this session"
        update = offered.updates["suggestion:0"]
        # Security: never the user's settings files, whatever Claude Code proposed.
        assert update.destination == "session"
        assert update.rules == BASH_RULE.rules

    def test_in_ask_a_rule_would_not_do_what_it_says(self) -> None:
        # Ask puts every change to the user whatever the rules say.
        assert choices([BASH_RULE], "default").options == ()

    def test_a_mode_is_offered_only_if_this_adapter_offers_it(self) -> None:
        bypass = PermissionUpdate(type="setMode", mode="bypassPermissions", destination="session")
        folder = PermissionUpdate(
            type="addDirectories", directories=["/etc"], destination="session"
        )
        offered = choices([ACCEPT_EDITS, bypass, folder], "default")
        assert [o.label for o in offered.options] == [
            "Allow",
            "Allow, and accept file edits from now on (Accept edits)",
            "Deny",
        ]
        assert offered.options[-1] == ConfirmationOption(
            id="deny", label="Deny", kind="deny", group=2
        )

    def test_nothing_usable_is_a_plain_prompt(self) -> None:
        assert choices([], "auto").options == ()

    def test_only_an_explicit_pick_applies_anything(self) -> None:
        offered = choices([BASH_RULE], "auto")
        plain = ToolConfirmationOutcome(approved=True)
        picked = ToolConfirmationOutcome(approved=True, selected_option=offered.options[1])
        once = ToolConfirmationOutcome(approved=True, selected_option=offered.options[0])
        assert offered.chosen(plain) is None
        assert offered.chosen(once) is None
        assert offered.chosen(picked) is offered.updates["suggestion:0"]


class TestDenials:
    def test_the_reason_and_the_suggestion_reach_claude(self) -> None:
        outcome = ToolConfirmationOutcome(
            approved=False,
            reason="denied",
            reason_message="not on main",
            user_suggestion=UserMessage(text="use a branch", raw={"attachments": [{}]}),
        )
        assert denial_message(outcome) == (
            "The user declined this tool call. Their reason: not on main "
            "What they would like instead: use a branch "
            "(They attached something to that suggestion, which is not shown here.)"
        )

    def test_a_skip_says_so(self) -> None:
        outcome = ToolConfirmationOutcome(approved=False, reason="skipped")
        assert denial_message(outcome) == "The user skipped this tool call."


class TestPreviews:
    def test_an_edit_is_shown_applied_to_the_file(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
        change = preview(
            "Edit",
            {"file_path": "a.py", "old_string": "y = 2", "new_string": "y = 3"},
            cwd=tmp_path,
            roots=Roots.single(tmp_path),
        )
        assert change is not None
        assert change.uri == (tmp_path / "a.py").resolve().as_uri()
        assert (change.before, change.after) == (b"x = 1\ny = 2\n", b"x = 1\ny = 3\n")

    def test_line_endings_are_kept(self, tmp_path: Path) -> None:
        (tmp_path / "w.txt").write_bytes(b"a\r\nb\r\n")
        change = preview(
            "Edit",
            {"file_path": "w.txt", "old_string": "a\nb", "new_string": "a\nc"},
            cwd=tmp_path,
            roots=Roots.single(tmp_path),
        )
        assert change is not None
        assert change.after == b"a\r\nc\r\n"

    def test_what_cannot_be_told_exactly_is_not_shown(self, tmp_path: Path) -> None:
        roots = Roots.single(tmp_path / "served")
        (tmp_path / "served").mkdir()
        (tmp_path / "served" / "twice.txt").write_text("ab ab")
        (tmp_path / "outside.txt").write_text("x")
        twice = {"file_path": "twice.txt", "old_string": "ab", "new_string": "cd"}
        missing = {"file_path": "twice.txt", "old_string": "zz", "new_string": "cd"}
        outside = {"file_path": str(tmp_path / "outside.txt"), "content": "y"}
        cwd = tmp_path / "served"
        assert preview("Edit", twice, cwd=cwd, roots=roots) is None
        assert preview("Edit", missing, cwd=cwd, roots=roots) is None
        assert preview("Write", outside, cwd=cwd, roots=roots) is None
        assert preview("NotebookEdit", {"notebook_path": "n.ipynb"}, cwd=cwd, roots=roots) is None
        every = {**twice, "replace_all": True}
        change = preview("Edit", every, cwd=cwd, roots=roots)
        assert change is not None
        assert change.after == b"cd cd"

    def test_a_new_file_has_nothing_before(self, tmp_path: Path) -> None:
        change = preview(
            "Write",
            {"file_path": "new.md", "content": "hi"},
            cwd=tmp_path,
            roots=Roots.single(tmp_path),
        )
        assert change is not None
        assert (change.before, change.after) == (None, b"hi")

    def test_multi_edit_applies_each_in_order(self, tmp_path: Path) -> None:
        (tmp_path / "m.txt").write_text("one two")
        edits = [
            {"old_string": "one", "new_string": "1"},
            {"old_string": "two", "new_string": "2"},
        ]
        change = preview(
            "MultiEdit",
            {"file_path": "m.txt", "edits": edits},
            cwd=tmp_path,
            roots=Roots.single(tmp_path),
        )
        assert change is not None
        assert change.after == b"1 2"


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )


def _asking(tool: str, tool_input: dict[str, Any], results: list[Any], **context: Any) -> Step:
    async def step(options: ClaudeAgentOptions) -> None:
        assert options.can_use_tool is not None
        results.append(
            await options.can_use_tool(
                tool, tool_input, ToolPermissionContext(tool_use_id="c1", **context)
            )
        )

    return step


async def _turn(
    tmp_path: Path, sink: RecordingSink, step: Step, *, mode: str = "default", tool: str = "Bash"
) -> tuple[Any, FakePublisher]:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    publisher = FakePublisher()
    provider = ClaudeProvider(
        tmp_path,
        client_factory=lambda options: FakeClient(
            options,
            [[AssistantMessage([ToolUseBlock("c1", tool, {})], model="m"), step, _result()]],
        ),
    )
    session = await provider.create_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            working_directories=[work.as_uri()],
            config={"permissionMode": mode},
            publisher=publisher,
        )
    )
    await session.send_user_message(UserMessage(text="go"), sink)
    return session, publisher


async def test_a_picked_rule_is_applied_for_the_session(tmp_path: Path) -> None:
    results: list[Any] = []
    sink = RecordingSink()
    offered = choices([BASH_RULE], "acceptEdits")
    sink.outcome = ToolConfirmationOutcome(approved=True, selected_option=offered.options[1])
    await _turn(
        tmp_path,
        sink,
        _asking("Bash", {"command": "npm test"}, results, suggestions=[BASH_RULE]),
        mode="acceptEdits",
    )
    (confirmation,) = sink.confirmations
    assert [o.id for o in confirmation.options] == ["allow", "suggestion:0", "deny"]
    (result,) = results
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions is not None
    (update,) = result.updated_permissions
    assert (update.type, update.destination) == ("addRules", "session")


async def test_picking_a_mode_moves_the_gate_with_it(tmp_path: Path) -> None:
    """Security-relevant: the user chose it, explicitly, on this prompt."""
    results: list[Any] = []
    sink = RecordingSink()
    offered = choices([ACCEPT_EDITS], "default")
    sink.outcome = ToolConfirmationOutcome(approved=True, selected_option=offered.options[1])
    session, publisher = await _turn(
        tmp_path,
        sink,
        _asking("Edit", {"file_path": "a.py"}, results, suggestions=[ACCEPT_EDITS]),
        tool="Edit",
    )
    assert session.approvals == "acceptEdits"
    assert {"permissionMode": "acceptEdits"} in publisher.config_changes


async def test_a_plain_approval_applies_nothing(tmp_path: Path) -> None:
    results: list[Any] = []
    await _turn(
        tmp_path,
        RecordingSink(),
        _asking("Bash", {"command": "ls"}, results, suggestions=[BASH_RULE]),
        mode="acceptEdits",
    )
    (result,) = results
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions is None


async def test_a_denial_tells_claude_why(tmp_path: Path) -> None:
    results: list[Any] = []
    sink = RecordingSink()
    sink.outcome = ToolConfirmationOutcome(
        approved=False, reason="denied", reason_message="wrong folder"
    )
    await _turn(tmp_path, sink, _asking("Bash", {"command": "rm x"}, results))
    (result,) = results
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "The user declined this tool call. Their reason: wrong folder"


async def test_an_edit_is_previewed_on_its_prompt(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "a.py").write_text("old\n")
    results: list[Any] = []
    sink = RecordingSink()
    edit = {"file_path": "a.py", "old_string": "old", "new_string": "new"}
    await _turn(tmp_path, sink, _asking("Edit", edit, results), tool="Edit")
    (confirmation,) = sink.confirmations
    (change,) = confirmation.edits
    assert (change.before, change.after) == (b"old\n", b"new\n")
