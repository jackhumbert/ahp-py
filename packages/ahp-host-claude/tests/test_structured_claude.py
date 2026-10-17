"""Structured turns: the host's per-turn tool, offered to Claude through the `host` server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import (
    HOST_CLIENT_ID,
    AgentSessionContext,
    HostTool,
    ReturnsStructuredOutput,
    ToolResult,
    UserMessage,
)
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock

from ahp_host_claude.provider import ClaudeProvider, ClaudeSession
from tests.fakes import FakeClient, RecordingSink, Step

TOOL = HostTool(
    name="StructuredOutput",
    title="Structured output",
    description="Return the result",
    input_schema={
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    },
)
OTHER = HostTool(
    name="StructuredOutput",
    title="Structured output",
    description="Return the result",
    input_schema={
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    },
)
NAME = "mcp__host__StructuredOutput"


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=5,
        duration_api_ms=4,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )


def _context() -> AgentSessionContext:
    return AgentSessionContext(
        session_uri="claude:/s",
        chat_uri="claude:/s/chat",
        provider_id="claude",
    )


class Harness:
    def __init__(self, tmp_path: Path, *turns: list[Step]) -> None:
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.turns = list(turns)
        self.clients: list[FakeClient] = []
        self.provider = ClaudeProvider(
            self.root, client_factory=self._factory, chat_dir=tmp_path / "chat"
        )

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.turns)
        self.clients.append(client)
        return client


class _HostCall:
    """What Claude Code does for the host tool: the hook, then the MCP call."""

    def __init__(
        self, session: ClaudeSession, call_id: str, arguments: dict[str, Any], tool: HostTool = TOOL
    ) -> None:
        self.session, self.call_id, self.arguments, self.tool = session, call_id, arguments, tool
        self.decision: Any = None
        self.result: Any = None

    async def __call__(self, options: ClaudeAgentOptions) -> None:
        hook: Any = options.hooks["PreToolUse"][0].hooks[0]  # type: ignore[index]
        hook_input = {"tool_name": NAME, "tool_input": self.arguments}
        self.decision = await hook(hook_input, self.call_id, None)
        self.result = await self.session._run_host_tool(self.tool, self.arguments)


def _use(arguments: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id="toolu_1", name=NAME, input=arguments)], model="m"
    )


async def test_a_local_session_returns_structured_output_and_a_mirror_does_not(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    session = await harness.provider.create_session(_context())
    assert isinstance(session, ReturnsStructuredOutput)
    assert session.offers_host_tools() is True
    session.mirror_of = object()  # type: ignore[assignment]
    assert session.offers_host_tools() is False


async def test_the_host_tool_is_served_allowed_and_run_by_the_host(tmp_path: Path) -> None:
    """Security-relevant: the gate lets the host's own tool through, because it
    runs in the host. The host announces the call, not this machine."""
    harness = Harness(tmp_path)
    session = await harness.provider.create_session(_context())
    arguments = {"answer": 42}
    step = _HostCall(session, "toolu_1", arguments)
    harness.turns.append([_use(arguments), step, _result()])
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="answer", host_tools=(TOOL,)), sink)

    servers: Any = harness.clients[0].options.mcp_servers
    assert servers["host"]["type"] == "sdk"
    assert step.decision["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert sink.confirmations == []
    [call] = sink.client_calls
    assert (call.call_id, call.name, call.client_id) == (
        "toolu_1",
        "StructuredOutput",
        HOST_CLIENT_ID,
    )
    assert call.tool_input == arguments
    assert [e[0] for e in sink.events if e[0] == "started"] == []


async def test_a_declined_host_call_is_an_error_claude_can_read(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.provider.create_session(_context())
    step = _HostCall(session, "toolu_1", {"answer": "x"})
    harness.turns.append([step, _result()])
    sink = RecordingSink()
    sink.client_result = ToolResult(response="decline", reason="bad")
    await session.send_user_message(UserMessage(text="answer", host_tools=(TOOL,)), sink)
    assert step.result == {"content": [{"type": "text", "text": "bad"}], "is_error": True}


async def test_a_new_tool_or_schema_restarts_claude_and_nothing_else_does(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()], [_result()], [_result()], [_result()])
    session = await harness.provider.create_session(_context())
    await session.send_user_message(UserMessage(text="1", host_tools=(TOOL,)), RecordingSink())
    await session.send_user_message(UserMessage(text="2", host_tools=(TOOL,)), RecordingSink())
    assert len(harness.clients) == 1, "the same tool needs no restart"

    await session.send_user_message(UserMessage(text="3", host_tools=(OTHER,)), RecordingSink())
    assert len(harness.clients) == 2
    assert harness.clients[0].disconnected
    new: Any = harness.clients[1].options.mcp_servers
    assert new["host"]["type"] == "sdk"
    assert harness.clients[1].options.resume == "abc"

    stale = _HostCall(session, "toolu_9", {"answer": 1})
    harness.turns[:] = [[stale, _result()]]
    await session.send_user_message(UserMessage(text="4"), RecordingSink())
    assert len(harness.clients) == 2, "a turn needing nothing new restarts nothing"
    assert stale.result["is_error"] is True
    assert "is not offered in this turn" in stale.result["content"][0]["text"]


async def test_a_follow_up_continues_the_turn_without_a_new_mark(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()], [_result()])
    session = await harness.provider.create_session(_context())
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="first", host_tools=(TOOL,)), sink)
    marks = len(session.marks)
    assert marks == 1
    await session.send_user_message(
        UserMessage(text="reply with only JSON", host_tools=(TOOL,), continues_turn=True), sink
    )
    assert len(session.marks) == marks


async def test_a_session_without_host_tools_has_no_server(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context())
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    assert harness.clients[0].options.mcp_servers == {}
