"""The adapter against a scripted SDK: no network, no subprocess."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import (
    AgentProvider,
    AgentSessionContext,
    ModelSelection,
    ResumableAgentProvider,
    UserMessage,
)
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk import UserMessage as SdkUserMessage
from claude_agent_sdk.types import StreamEvent

from ahp_host_claude.paths import directory_of
from ahp_host_claude.permissions import needs_approval, pre_tool_use_decision
from ahp_host_claude.provider import ClaudeProvider, ClaudeSession
from tests.fakes import FakeClient, RecordingSink, Step, text_of


def _stream(event: dict[str, Any], parent: str | None = None) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s1", event=event, parent_tool_use_id=parent)


def _text_delta(text: str) -> StreamEvent:
    return _stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})


def _result(**overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 5,
        "duration_api_ms": 4,
        "is_error": False,
        "num_turns": 1,
        "session_id": "claude-session-1",
        "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 7},
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def _context(tmp_path: Path, *dirs: str) -> AgentSessionContext:
    return AgentSessionContext(
        session_uri="claude:/s",
        chat_uri="claude:/s/chat",
        provider_id="claude",
        working_directories=dirs,
    )


class Harness:
    """A provider whose client factory hands out FakeClients with a script."""

    def __init__(self, root: Path, *turns: list[Step]) -> None:
        self.turns = list(turns)
        self.clients: list[FakeClient] = []
        self.provider = ClaudeProvider(root, client_factory=self._factory)

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.turns)
        self.clients.append(client)
        return client


def _ask(tool: str, tool_input: dict[str, Any], call_id: str, results: list[Any]) -> Step:
    """A step where the CLI asks permission, as it does before running a tool."""

    async def step(options: ClaudeAgentOptions) -> None:
        assert options.can_use_tool is not None
        context = ToolPermissionContext(tool_use_id=call_id, title=f"Claude wants to use {tool}")
        results.append(await options.can_use_tool(tool, tool_input, context))

    return step


async def test_the_provider_is_a_resumable_agent_provider(tmp_path: Path) -> None:
    provider = ClaudeProvider(tmp_path)
    assert isinstance(provider, AgentProvider)
    assert isinstance(provider, ResumableAgentProvider)
    assert provider.agent.provider == "claude"


async def test_text_streams_as_deltas_and_is_not_repeated_by_the_final_message(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path,
        [
            SystemMessage(subtype="init", data={"session_id": "claude-session-1"}),
            _stream({"type": "message_start", "message": {"id": "m1"}}),
            _text_delta("Hel"),
            _text_delta("lo"),
            AssistantMessage(content=[TextBlock("Hello")], model="claude-opus-5", message_id="m1"),
            _result(),
        ],
    )
    session = await harness.provider.create_session(_context(tmp_path))
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="hi"), sink)

    assert sink.events == [
        ("text", "Hel"),
        ("text", "lo"),
        # The prompt, cached part included: 10 uncached + 7 read from cache.
        ("usage", 17, 3, 7, "claude-opus-5"),
    ]
    assert [text_of(p) for p in harness.clients[0].prompts] == ["hi"]
    assert session.claude_session_id == "claude-session-1"


async def test_an_unstreamed_message_is_published_whole(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [AssistantMessage(content=[TextBlock("whole")], model="m", message_id="m9"), _result()],
    )
    session = await harness.provider.create_session(_context(tmp_path))
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="hi"), sink)
    assert ("text", "whole") in sink.events


async def test_a_read_only_tool_runs_without_asking(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [
            AssistantMessage(
                content=[ToolUseBlock("t1", "Read", {"file_path": "/x/a.py"})], model="m"
            ),
            SdkUserMessage(content=[ToolResultBlock("t1", "print(1)", False)]),
            _result(),
        ],
    )
    session = await harness.provider.create_session(_context(tmp_path))
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="read it"), sink)

    assert ("started", "t1", "Read", "Read file") in sink.events
    completed = [e for e in sink.events if e[0] == "completed"]
    assert completed == [
        ("completed", "t1", True, {"content": [{"type": "text", "text": "print(1)"}]})
    ]
    assert sink.confirmations == []


async def test_a_shell_command_is_confirmed_by_the_client_first(tmp_path: Path) -> None:
    results: list[Any] = []
    harness = Harness(
        tmp_path,
        [
            AssistantMessage(content=[ToolUseBlock("t2", "Bash", {"command": "ls"})], model="m"),
            _ask("Bash", {"command": "ls"}, "t2", results),
            SdkUserMessage(content=[ToolResultBlock("t2", "a.py", False)]),
            _result(),
        ],
    )
    session = await harness.provider.create_session(_context(tmp_path))
    sink = RecordingSink(approve=True)
    await session.send_user_message(UserMessage(text="list"), sink)

    assert [c.call_id for c in sink.confirmations] == ["t2"]
    assert sink.confirmations[0].tool_input == {"command": "ls"}
    assert isinstance(results[0], PermissionResultAllow)
    # Announced exactly once, before the confirmation.
    kinds = [e[0] for e in sink.events if e[0] in ("started", "confirm")]
    assert kinds == ["started", "confirm"]


async def test_a_declined_call_is_denied_to_claude(tmp_path: Path) -> None:
    results: list[Any] = []
    harness = Harness(
        tmp_path,
        [
            _ask("Bash", {"command": "rm -rf build"}, "t3", results),
            SdkUserMessage(content=[ToolResultBlock("t3", "denied", True)]),
            _result(),
        ],
    )
    session = await harness.provider.create_session(_context(tmp_path))
    sink = RecordingSink(approve=False)
    await session.send_user_message(UserMessage(text="clean"), sink)

    assert isinstance(results[0], PermissionResultDeny)
    assert ("completed", "t3", False, {"content": [{"type": "text", "text": "denied"}]}) in (
        sink.events
    )


async def test_an_edited_approval_runs_the_edited_input(tmp_path: Path) -> None:
    results: list[Any] = []
    harness = Harness(tmp_path, [_ask("Bash", {"command": "rm -rf /"}, "t4", results), _result()])
    session = await harness.provider.create_session(_context(tmp_path))
    sink = RecordingSink(approve=True, edited_input={"command": "echo safe"})
    await session.send_user_message(UserMessage(text="x"), sink)

    assert isinstance(results[0], PermissionResultAllow)
    assert results[0].updated_input == {"command": "echo safe"}


async def test_an_error_result_fails_the_turn(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, [_result(is_error=True, subtype="error_during_execution", errors=["boom"])]
    )
    session = await harness.provider.create_session(_context(tmp_path))
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="x"), sink)
    assert ("failed", "boom", "claude.error_during_execution") in sink.events


async def test_an_interrupted_turn_is_not_reported_as_a_failure(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [_result(is_error=True, subtype="error_during_execution", terminal_reason="aborted_tools")],
    )
    session = await harness.provider.create_session(_context(tmp_path))
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="x"), sink)
    assert not [e for e in sink.events if e[0] == "failed"]


async def test_cancel_interrupts_the_client(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context(tmp_path))
    await session.send_user_message(UserMessage(text="x"), RecordingSink())
    await session.cancel()
    await session.aclose()
    assert harness.clients[0].interrupted
    assert harness.clients[0].disconnected


async def test_a_model_pick_is_applied(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context(tmp_path))
    message = UserMessage(text="x", model=ModelSelection(id="claude-sonnet-5"))
    await session.send_user_message(message, RecordingSink())
    assert harness.clients[0].models == ["claude-sonnet-5"]


async def test_sessions_resume_with_the_sdk_session_id(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result(session_id="abc")], [_result(session_id="abc")])
    session = await harness.provider.create_session(_context(tmp_path))
    await session.send_user_message(UserMessage(text="x"), RecordingSink())
    state = await harness.provider.resume_state_of(session)
    assert state is not None
    assert len(state["turns"]) == 1
    assert {key: value for key, value in state.items() if key != "turns"} == {
        "claudeSessionId": "abc",
        "permissionMode": "default",
        "remoteControl": False,
        # Pinned at its first start: the conversation is stored under it.
        "cwd": str((tmp_path / "state" / "chat").resolve()),
    }

    context = _context(tmp_path)
    resumed_context = AgentSessionContext(
        session_uri=context.session_uri,
        chat_uri=context.chat_uri,
        provider_id=context.provider_id,
        resume_state=state,
    )
    resumed = await harness.provider.resume_session(resumed_context)
    await resumed.send_user_message(UserMessage(text="again"), RecordingSink())
    assert harness.clients[1].options.resume == "abc"


async def test_the_working_directory_comes_from_the_session(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context(tmp_path, project.as_uri()))
    await session.send_user_message(UserMessage(text="x"), RecordingSink())
    assert harness.clients[0].options.cwd == str(project.resolve())


async def test_a_directory_outside_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    harness = Harness(root, [_result()])
    session = await harness.provider.create_session(_context(root, tmp_path.as_uri()))
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="x"), sink)
    assert [e[0] for e in sink.events] == ["failed"]
    assert harness.clients == []


def test_vscode_agent_host_uris_are_local_paths() -> None:
    assert directory_of("vscode-agent-host://my-host/Users/me/p") == Path("/Users/me/p")
    assert directory_of("file:///Users/me/a%20b") == Path("/Users/me/a b")
    assert directory_of("https://example.com/x") is None


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "LS", "TodoWrite", "Task"])
def test_read_only_tools_are_allowed(tool: str) -> None:
    assert not needs_approval(tool)
    decision = pre_tool_use_decision(tool)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"


@pytest.mark.parametrize(
    "tool",
    ["Bash", "Edit", "MultiEdit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "mcp__x"],
)
def test_everything_else_asks(tool: str) -> None:
    assert needs_approval(tool)
    decision = pre_tool_use_decision(tool)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_session_options_gate_every_tool_through_the_hook(tmp_path: Path) -> None:
    session = ClaudeSession(_context(tmp_path), root=tmp_path, client_factory=lambda o: None)  # type: ignore[arg-type,return-value]
    options = session._options()
    matchers = options.hooks["PreToolUse"] if options.hooks else []
    assert len(matchers) == 1
    assert matchers[0].matcher is None  # every tool, not a named subset
    assert options.permission_mode == "default"
    assert options.allowed_tools == []
    # Claude's question tool is answered through an input request now.
    assert "AskUserQuestion" not in options.disallowed_tools
    # A subagent's prose reaches its worker chat, not only its tool calls.
    assert options.forward_subagent_text is True


async def test_a_shell_call_reads_as_what_it_does_not_as_run_command(tmp_path: Path) -> None:
    # Without a line of its own, a client shows the host's fallback "Running Run
    # command" and then "Done" for every shell call - the command is only in the
    # raw input. Claude's own `description` says what the call is for.
    command = {"command": "git ls-files | wc -l", "description": "Count tracked files"}
    harness = Harness(
        tmp_path,
        [
            AssistantMessage(content=[ToolUseBlock("t4", "Bash", command)], model="m"),
            SdkUserMessage(content=[ToolResultBlock("t4", "42", False)]),
            AssistantMessage(
                content=[ToolUseBlock("t5", "Read", {"file_path": "/x/a.py"})], model="m"
            ),
            SdkUserMessage(content=[ToolResultBlock("t5", "nope", True)]),
            _result(),
        ],
    )
    session = await harness.provider.create_session(_context(tmp_path))
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="count"), sink)

    assert sink.invocations == {"t4": "Count tracked files", "t5": "Read file: a.py"}
    assert sink.past_tense == {"t4": "Ran `git ls-files | wc -l`", "t5": "Failed: Read file: a.py"}


def test_sessions_are_recorded_so_claude_resume_lists_them(tmp_path: Path) -> None:
    """`claude --resume` hides sessions whose entrypoint is `sdk-*`, which is
    what the SDK sets; `cli` would be rewritten to `sdk-cli`."""
    session = ClaudeSession(_context(tmp_path), root=tmp_path, client_factory=lambda o: None)  # type: ignore[arg-type,return-value]
    entrypoint = session._options().env["CLAUDE_CODE_ENTRYPOINT"]
    assert not entrypoint.startswith("sdk-")
    assert entrypoint != "cli"
