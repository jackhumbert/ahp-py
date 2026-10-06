"""Claude Code's background tasks, shown as chat background work (1.0.0).

Messages are built with the SDK's own parser from raw CLI frames, so the tests
exercise the typed `Task*Message` subclasses the real stream produces.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, UserMessage
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)
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


def _background_task(task_id: str = "a1", call: str = "t1") -> list[Step]:
    return [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    call,
                    "Task",
                    {"prompt": "map the repo", "description": "Explore", "run_in_background": True},
                )
            ],
            model="m",
        ),
        _started(
            task_id,
            "local_agent",
            tool_use_id=call,
            is_backgrounded=True,
            description="Explore",
            subagent_type="Explore",
        ),
        SdkUserMessage(content=[ToolResultBlock(call, "Async agent launched", False)]),
        _result(),
    ]


async def test_a_background_subagent_gets_a_worker_chat_and_an_entry(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _background_task())
    session = await harness.provider.create_session(harness.context())
    parent = RecordingSink(approve=True)
    await session.send_user_message(UserMessage(text="explore"), parent)

    (chat,) = harness.publisher.chats
    assert chat.tool_call_id == "t1"
    assert chat.title == "Explore"
    assert chat.prompts == ["map the repo"]
    work = harness.publisher.background["local_agent:a1"]
    assert work["kind"] == "subagent"
    assert work["chat"] == chat.resource
    assert work["_meta"] == {"taskId": "a1", "agentType": "Explore"}
    # The spawning call's result points at the worker chat.
    (completed,) = [e for e in parent.events if e[0] == "completed"]
    assert {"type": "subagent", "resource": chat.resource, "title": "Explore"} in completed[3][
        "content"
    ]


async def test_the_subagents_messages_stream_into_its_chat_after_the_turn(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, _background_task())
    session = await harness.provider.create_session(harness.context())
    parent = RecordingSink(approve=True)
    await session.send_user_message(UserMessage(text="explore"), parent)
    (chat,) = harness.publisher.chats

    client = harness.clients[0]
    client.push(
        AssistantMessage(
            content=[
                TextBlock("Looking around."),
                ToolUseBlock("s1", "Read", {"file_path": "/x/a.py"}),
            ],
            model="m",
            parent_tool_use_id="t1",
        ),
        SdkUserMessage(content=[ToolResultBlock("s1", "print(1)", False)], parent_tool_use_id="t1"),
    )
    await eventually(
        lambda: bool(chat.sinks) and any(e[0] == "completed" for e in chat.sinks[0].events)
    )
    worker = chat.sinks[0]
    assert ("text", "Looking around.") in worker.events
    assert ("started", "s1", "Read", "Read file") in worker.events
    # None of it leaked into the parent turn.
    assert not any(e[0] == "started" and e[1] == "s1" for e in parent.events)

    client.push(
        _system(
            "task_notification",
            task_id="a1",
            status="completed",
            output_file="/tmp/a1",
            summary="done",
        )
    )
    await eventually(lambda: chat.tasks[0].done())
    assert "local_agent:a1" not in harness.publisher.background


def _foreground_task(*, started_first: bool = True) -> list[Step]:
    spawn = AssistantMessage(
        content=[ToolUseBlock("t1", "Agent", {"prompt": "look", "description": "Survey"})],
        model="m",
    )
    started = _started(
        "a2", "local_agent", tool_use_id="t1", is_backgrounded=False, description="Survey"
    )
    inner = [
        AssistantMessage(
            content=[TextBlock("Looking."), ToolUseBlock("s1", "Read", {"file_path": "/x/a.py"})],
            model="m",
            parent_tool_use_id="t1",
        ),
        SdkUserMessage(content=[ToolResultBlock("s1", "print(1)", False)], parent_tool_use_id="t1"),
    ]
    steps: list[Step] = [spawn, started, *inner] if started_first else [spawn, *inner, started]
    return [
        *steps,
        SdkUserMessage(content=[ToolResultBlock("t1", "It is a small repo.", False)]),
        _result(),
    ]


async def test_a_foreground_subagent_gets_a_worker_chat_too(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _foreground_task())
    session = await harness.provider.create_session(harness.context())
    parent = RecordingSink(approve=True)
    await session.send_user_message(UserMessage(text="survey"), parent)

    (chat,) = harness.publisher.chats
    assert chat.tool_call_id == "t1"
    assert chat.prompts == ["look"]
    await eventually(lambda: bool(chat.tasks) and chat.tasks[0].done())
    worker = chat.sinks[0]
    assert ("text", "Looking.") in worker.events
    assert ("started", "s1", "Read", "Read file") in worker.events
    assert any(e[0] == "completed" and e[1] == "s1" for e in worker.events)
    # Its calls are in its chat, not inline in the parent's turn.
    assert not any(e[0] == "started" and e[1] == "s1" for e in parent.events)
    # The spawning call links to the chat, though the subagent has ended.
    (completed,) = [e for e in parent.events if e[0] == "completed"]
    assert {"type": "subagent", "resource": chat.resource, "title": "Survey"} in completed[3][
        "content"
    ]
    # Not background work: it ran while the parent waited for it.
    assert harness.publisher.background == {}


async def test_a_subagents_first_message_may_come_before_its_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _foreground_task(started_first=False))
    session = await harness.provider.create_session(harness.context())
    parent = RecordingSink(approve=True)
    await session.send_user_message(UserMessage(text="survey"), parent)
    (chat,) = harness.publisher.chats
    await eventually(lambda: bool(chat.tasks) and chat.tasks[0].done())
    assert ("text", "Looking.") in chat.sinks[0].events
    assert not any(e[0] == "started" and e[1] == "s1" for e in parent.events)


async def test_a_subagents_approval_is_asked_in_its_own_chat(tmp_path: Path) -> None:
    asked: list[Any] = []

    async def ask(options: ClaudeAgentOptions) -> None:
        assert options.can_use_tool is not None
        context = ToolPermissionContext(tool_use_id="s2", agent_id="a2")
        asked.append(await options.can_use_tool("Bash", {"command": "ls"}, context))

    script = _foreground_task()
    script.insert(4, ask)  # after the subagent's first messages
    harness = Harness(tmp_path, script)
    session = await harness.provider.create_session(harness.context())
    parent = RecordingSink(approve=True)
    await session.send_user_message(UserMessage(text="survey"), parent)
    (chat,) = harness.publisher.chats
    worker = chat.sinks[0]
    assert [c.call_id for c in worker.confirmations] == ["s2"]
    assert ("started", "s2", "Bash", "Run command") in worker.events
    assert parent.confirmations == []
    assert isinstance(asked[0], PermissionResultAllow)


async def test_stopping_a_foreground_worker_stops_only_its_task(tmp_path: Path) -> None:
    script = _foreground_task()
    script = script[:-2]  # the subagent never finishes on its own
    harness = Harness(tmp_path, script)
    session = await harness.provider.create_session(harness.context())
    turn = asyncio.create_task(session.send_user_message(UserMessage(text="x"), RecordingSink()))
    await eventually(
        lambda: bool(harness.publisher.chats) and bool(harness.publisher.chats[0].tasks)
    )
    chat = harness.publisher.chats[0]
    chat.tasks[0].cancel()
    await eventually(lambda: harness.clients[0].stopped_tasks == ["a2"])
    assert not harness.clients[0].interrupted
    turn.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await turn


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


async def test_a_background_subagent_is_approved_with_no_parent_turn(tmp_path: Path) -> None:
    """Its prompt used to need a turn running in the parent, and was denied without."""
    harness = Harness(tmp_path, _background_task())
    session = await harness.provider.create_session(harness.context())
    await session.send_user_message(UserMessage(text="explore"), RecordingSink(approve=True))
    (chat,) = harness.publisher.chats
    await eventually(lambda: bool(chat.sinks))
    asked: list[Any] = []

    async def ask(options: ClaudeAgentOptions) -> None:
        assert options.can_use_tool is not None
        context = ToolPermissionContext(tool_use_id="s9", agent_id="a1")
        asked.append(await options.can_use_tool("Bash", {"command": "make"}, context))

    harness.clients[0].push(ask)
    await eventually(lambda: bool(asked))
    assert isinstance(asked[0], PermissionResultAllow)
    assert [c.call_id for c in chat.sinks[0].confirmations] == ["s9"]
