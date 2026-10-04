"""Claude Code's background tasks, shown as chat background work (1.0.0).

Messages are built with the SDK's own parser from raw CLI frames, so the tests
exercise the typed `Task*Message` subclasses the real stream produces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, UserMessage
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock
from claude_agent_sdk import UserMessage as SdkUserMessage
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import ToolResultBlock

from ahp_host_claude.background import Task, work_for
from ahp_host_claude.provider import ClaudeProvider
from tests.fakes import FakeClient, FakePublisher, RecordingSink, Step, eventually

_BASH = {"command": "npm run dev", "run_in_background": True, "description": "Start dev server"}


def _system(subtype: str, **fields: Any) -> Any:
    return parse_message(
        {"type": "system", "subtype": subtype, "uuid": "u", "session_id": "s1", **fields}
    )


def _started(task_id: str, task_type: str, **fields: Any) -> Any:
    return _system(
        "task_started",
        task_id=task_id,
        task_type=task_type,
        description=fields.pop("description", "Start dev server"),
        **fields,
    )


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=5,
        duration_api_ms=4,
        is_error=False,
        num_turns=1,
        session_id="s1",
    )


class Harness:
    def __init__(self, root: Path, *turns: list[Step]) -> None:
        self.clients: list[FakeClient] = []
        self.turns = list(turns)
        self.publisher = FakePublisher()
        self.provider = ClaudeProvider(root, client_factory=self._factory)

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.turns)
        self.clients.append(client)
        return client

    def context(self) -> AgentSessionContext:
        return AgentSessionContext(
            session_uri="claude:/s",
            chat_uri="claude:/s/chat",
            provider_id="claude",
            publisher=self.publisher,
        )


def _backgrounded_bash() -> list[Step]:
    return [
        AssistantMessage(content=[ToolUseBlock("t1", "Bash", _BASH)], model="m"),
        _started("b1", "local_bash", tool_use_id="t1"),
        SdkUserMessage(content=[ToolResultBlock("t1", "Running in background", False)]),
        _result(),
    ]


async def _run_turn(harness: Harness) -> Any:
    session = await harness.provider.create_session(harness.context())
    await session.send_user_message(UserMessage(text="start it"), RecordingSink(approve=True))
    return session


async def test_a_background_shell_is_published_with_its_command(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _backgrounded_bash())
    await _run_turn(harness)
    work = harness.publisher.background["local_bash:b1"]
    assert work["kind"] == "shell"
    assert work["command"] == "npm run dev"
    assert work["label"] == "Start dev server"
    assert work["_meta"] == {"attached": True, "taskId": "b1"}


async def test_it_outlives_the_turn_and_ends_on_its_notification(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _backgrounded_bash())
    await _run_turn(harness)
    assert "local_bash:b1" in harness.publisher.background, "the turn's end removed it"

    harness.clients[0].push(
        _system(
            "task_notification",
            task_id="b1",
            status="completed",
            output_file="/tmp/b1.out",
            summary="done",
        )
    )
    await eventually(lambda: not harness.publisher.background)


async def test_a_killed_task_ends_on_task_updated_alone(tmp_path: Path) -> None:
    """`TaskStop` may report only `task_updated` with `killed`."""
    harness = Harness(tmp_path, _backgrounded_bash())
    await _run_turn(harness)
    harness.clients[0].push(_system("task_updated", task_id="b1", patch={"status": "killed"}))
    await eventually(lambda: not harness.publisher.background)


async def test_a_task_moved_to_the_background_later_is_published_then(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [
            AssistantMessage(content=[ToolUseBlock("t1", "Bash", {"command": "make"})], model="m"),
            _started("b2", "local_bash", tool_use_id="t1", is_backgrounded=False),
            SdkUserMessage(content=[ToolResultBlock("t1", "moved", False)]),
            _result(),
        ],
    )
    await _run_turn(harness)
    assert harness.publisher.background == {}
    harness.clients[0].push(_system("task_updated", task_id="b2", patch={"is_backgrounded": True}))
    await eventually(lambda: "local_bash:b2" in harness.publisher.background)
    assert harness.publisher.background["local_bash:b2"]["command"] == "make"


async def test_a_subagent_is_not_published_without_a_chat(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [
            AssistantMessage(content=[ToolUseBlock("t1", "Task", {"prompt": "x"})], model="m"),
            _started("a1", "local_agent", tool_use_id="t1", is_backgrounded=True),
            SdkUserMessage(content=[ToolResultBlock("t1", "launched", False)]),
            _result(),
        ],
    )
    await _run_turn(harness)
    assert harness.publisher.background == {}


async def test_stopping_the_client_withdraws_its_shells(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _backgrounded_bash())
    session = await _run_turn(harness)
    assert harness.publisher.background
    await session.aclose()
    assert harness.publisher.background == {}


class TestWorkFor:
    def _task(self, **fields: Any) -> Task:
        base: dict[str, Any] = {
            "task_id": "x",
            "task_type": "local_bash",
            "description": "",
            "started_at": "2026-10-04T00:00:00.000Z",
        }
        return Task(**{**base, **fields})

    def test_a_shell_without_a_known_command_falls_back_to_its_description(self) -> None:
        work = work_for(self._task(description="Tail the log"))
        assert work is not None
        assert work.command == "Tail the log"

    def test_a_foreground_shell_gets_no_entry(self) -> None:
        assert work_for(self._task(backgrounded=False)) is None

    def test_an_unknown_task_type_gets_no_entry(self) -> None:
        assert work_for(self._task(task_type="dream")) is None
