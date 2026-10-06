"""Each edit's diff on its call, and the changeset of everything Claude edited."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, UserMessage
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock
from claude_agent_sdk import UserMessage as SdkUserMessage
from claude_agent_sdk.types import ToolResultBlock

from ahp_host_claude.provider import ClaudeProvider
from tests.fakes import FakeClient, FakePublisher, RecordingSink, Step


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )


def _edit(
    call: str, tool: str, tool_input: dict[str, Any], write: Any, *, failed: bool = False
) -> list[Step]:
    """Claude's call: the hook sees it, the file changes (or not), the result comes back."""

    async def hook_then_run(options: ClaudeAgentOptions) -> None:
        assert options.hooks is not None
        hook = options.hooks["PreToolUse"][0].hooks[0]
        hook_input: Any = {"tool_name": tool, "tool_input": tool_input}
        await hook(hook_input, call, {"signal": None})
        write()

    return [
        AssistantMessage([ToolUseBlock(call, tool, tool_input)], model="m"),
        hook_then_run,
        SdkUserMessage(content=[ToolResultBlock(call, "done", failed)]),
    ]


class Harness:
    def __init__(self, root: Path, *turns: list[Step]) -> None:
        self.root = root
        self.work = root / "work"
        self.work.mkdir(exist_ok=True)
        self.publisher = FakePublisher()
        self.turns = list(turns)
        self.provider = ClaudeProvider(
            root,
            client_factory=lambda options: FakeClient(options, self.turns),
        )

    async def run(self, sink: RecordingSink | None = None) -> RecordingSink:
        session = await self.provider.create_session(
            AgentSessionContext(
                session_uri="s",
                chat_uri="c",
                provider_id="claude",
                working_directories=[self.work.as_uri()],
                config={"permissionMode": "acceptEdits"},
                publisher=self.publisher,
            )
        )
        sink = sink or RecordingSink()
        await session.send_user_message(UserMessage(text="edit"), sink)
        return sink


async def test_an_edit_carries_its_diff_and_joins_the_changeset(tmp_path: Path) -> None:
    target = tmp_path / "work" / "a.py"
    Harness(tmp_path)  # makes the folder
    target.write_text("before\n")

    def write() -> None:
        target.write_text("after\n")

    harness = Harness(
        tmp_path,
        [*_edit("e1", "Edit", {"file_path": str(target)}, write), _result()],
    )
    sink = await harness.run()
    (change,) = sink.file_edits
    assert (change.before, change.after) == (b"before\n", b"after\n")
    uri = target.resolve().as_uri()
    (completed,) = [e for e in sink.events if e[0] == "completed"]
    assert {"type": "fileEdit", "uri": uri} in completed[3]["content"]
    # The session's changeset: one, refreshed in place, scoped to the session.
    changeset, changes, chat = harness.publisher.changesets[-1]
    assert chat is None
    assert changeset.reviewable
    assert [(c.uri, c.before, c.after) for c in changes] == [(uri, b"before\n", b"after\n")]


async def test_a_file_edited_twice_shows_first_before_and_last_after(tmp_path: Path) -> None:
    Harness(tmp_path)
    target = tmp_path / "work" / "b.txt"
    target.write_text("1")
    harness = Harness(
        tmp_path,
        [
            *_edit("e1", "Write", {"file_path": str(target)}, lambda: target.write_text("2")),
            *_edit("e2", "Write", {"file_path": str(target)}, lambda: target.write_text("3")),
            _result(),
        ],
    )
    sink = await harness.run()
    assert [(c.before, c.after) for c in sink.file_edits] == [(b"1", b"2"), (b"2", b"3")]
    _, changes, _ = harness.publisher.changesets[-1]
    assert [(c.before, c.after) for c in changes] == [(b"1", b"3")]
    # One changeset, refreshed: the same object every time.
    assert len({id(published) for published, _, _ in harness.publisher.changesets}) == 1


async def test_a_failed_call_shows_no_diff(tmp_path: Path) -> None:
    Harness(tmp_path)
    target = tmp_path / "work" / "c.txt"
    target.write_text("x")
    harness = Harness(
        tmp_path,
        [*_edit("e1", "Edit", {"file_path": str(target)}, lambda: None, failed=True), _result()],
    )
    sink = await harness.run()
    assert sink.file_edits == []
    assert harness.publisher.changesets == []


async def test_files_outside_the_served_folders_are_not_shown(tmp_path: Path) -> None:
    served = tmp_path / "served"
    served.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    harness = Harness(
        served,
        [
            *_edit("e1", "Write", {"file_path": str(outside)}, lambda: outside.write_text("y")),
            _result(),
        ],
    )
    sink = await harness.run()
    assert sink.file_edits == []


async def test_a_shell_command_editing_a_file_is_not_guessed_at(tmp_path: Path) -> None:
    Harness(tmp_path)
    target = tmp_path / "work" / "d.txt"
    target.write_text("x")
    harness = Harness(
        tmp_path,
        [
            *_edit("b1", "Bash", {"command": f"echo y > {target}"}, lambda: target.write_text("y")),
            _result(),
        ],
    )
    sink = await harness.run()
    assert sink.file_edits == []
