"""A client's own tools, offered to Claude and run by the client."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import (
    AgentSessionContext,
    FollowsActiveClients,
    ToolResult,
    UserMessage,
)
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock

from ahp_host_claude.client_tools import mcp_result, offered
from ahp_host_claude.provider import ClaudeProvider, ClaudeSession
from tests.fakes import FakeClient, RecordingSink, Step

_TOOLS = [
    {
        "name": "create_session",
        "title": "Start a session",
        "description": "Start a session on a machine",
        "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}}},
    },
]


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=5,
        duration_api_ms=4,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )


def _context(client_id: str | None = "gateway", tools: Any = _TOOLS) -> AgentSessionContext:
    return AgentSessionContext(
        session_uri="claude:/s",
        chat_uri="claude:/s/chat",
        provider_id="claude",
        active_client_id=client_id,
        client_tools=tuple(tools) if client_id is not None else (),
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


class _Call:
    """What Claude Code does for one client tool: the hook, then the MCP call."""

    def __init__(self, session: ClaudeSession, call_id: str, arguments: dict[str, Any]) -> None:
        self.session, self.call_id, self.arguments = session, call_id, arguments
        self.decision: Any = None
        self.result: Any = None

    async def __call__(self, options: ClaudeAgentOptions) -> None:
        hook: Any = options.hooks["PreToolUse"][0].hooks[0]  # type: ignore[index]
        hook_input = {"tool_name": "mcp__client__create_session", "tool_input": self.arguments}
        self.decision = await hook(hook_input, self.call_id, None)
        tool = self.session._client_tools[0]
        self.result = await self.session._run_client_tool(tool, self.arguments)


def test_each_published_tool_is_offered_once() -> None:
    """The first client in `activeClients` order runs a shared name; a name
    Claude cannot take is made safe."""
    tools = offered(
        [
            {"clientId": "a", "tools": [{"name": "x.y"}, {"name": "shared"}]},
            {"clientId": "b", "tools": [{"name": "shared"}, {"name": "own"}]},
            {"clientId": "c"},  # no tools at all
        ]
    )
    assert [(t.name, t.published, t.client_id) for t in tools] == [
        ("x_y", "x.y", "a"),
        ("shared", "shared", "a"),
        ("own", "own", "b"),
    ]
    # A bare object schema is kept an object schema, not read as parameters.
    assert tools[0].input_schema == {"type": "object", "properties": {}}


async def test_a_chat_gets_the_clients_tools(tmp_path: Path) -> None:
    """No folder means no tools on this machine - but a client's tools run in
    the client, so a chat has them."""
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context())
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    options = harness.clients[0].options
    assert options.strict_mcp_config is True
    assert isinstance(options.mcp_servers, dict)
    assert options.mcp_servers["client"]["type"] == "sdk"


async def test_a_session_without_client_tools_has_no_server(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context(client_id=None))
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    assert harness.clients[0].options.mcp_servers == {}


async def test_the_client_runs_the_call_under_claudes_id(tmp_path: Path) -> None:
    """Security-relevant: allowed without this host's approval, because the
    client that runs it decides. Announced once, by `run_client_tool`, as the
    client's call - never also as a call of this machine."""
    harness = Harness(tmp_path)
    session = await harness.provider.create_session(_context())
    arguments = {"node": "my-mac-mini"}
    step = _Call(session, "toolu_1", arguments)
    use = AssistantMessage(
        content=[ToolUseBlock(id="toolu_1", name="mcp__client__create_session", input=arguments)],
        model="m",
    )
    harness.turns.append([use, step, _result()])
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="start one"), sink)

    assert step.decision["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert sink.confirmations == []
    [call] = sink.client_calls
    assert (call.call_id, call.name, call.client_id) == ("toolu_1", "create_session", "gateway")
    assert call.tool_input == arguments
    assert call.display_name == "Start a session"
    assert [e[0] for e in sink.events if e[0] in ("started", "client_tool")] == ["client_tool"]
    assert step.result == {"content": [{"type": "text", "text": "ran"}]}


async def test_a_failed_call_is_an_error_claude_can_read(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.provider.create_session(_context())
    step = _Call(session, "toolu_1", {"node": "nowhere"})
    harness.turns.append([step, _result()])
    sink = RecordingSink()
    sink.client_result = ToolResult(response="decline", reason="no such node")
    await session.send_user_message(UserMessage(text="start one"), sink)
    assert step.result == {
        "content": [{"type": "text", "text": "no such node"}],
        "is_error": True,
    }


async def test_a_departed_client_is_an_error_not_a_hang(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.provider.create_session(_context())
    step = _Call(session, "toolu_1", {})
    harness.turns.append([step, _result()])
    sink = RecordingSink()
    sink.client_result = LookupError("'gateway' is not an active client of this session")
    await session.send_user_message(UserMessage(text="start one"), sink)
    assert step.result["is_error"] is True


async def test_the_session_follows_its_clients(tmp_path: Path) -> None:
    """The same tools from another client restart nothing but run there; new
    tools restart an idle client, resuming the conversation."""
    harness = Harness(tmp_path, [_result()], [_result()])
    session = await harness.provider.create_session(_context())
    assert isinstance(session, FollowsActiveClients)
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())

    await session.active_clients_changed(
        [{"clientId": "phone", "tools": []}, {"clientId": "gateway-2", "tools": _TOOLS}]
    )
    assert not harness.clients[0].disconnected
    assert session._client_tools[0].client_id == "gateway-2"

    more = [*_TOOLS, {"name": "send", "inputSchema": {"type": "object"}}]
    await session.active_clients_changed([{"clientId": "gateway-2", "tools": more}])
    assert harness.clients[0].disconnected, "an idle client restarts straight away"
    await session.send_user_message(UserMessage(text="again"), RecordingSink())
    assert harness.clients[1].options.resume == "abc"


def test_a_worker_the_tool_started_is_described() -> None:
    result = mcp_result(
        ToolResult(
            value={
                "success": True,
                "content": [
                    {
                        "type": "subagent",
                        "resource": "ahp-chat://node/1",
                        "title": "Fix the build",
                        "description": "on my-mac-mini",
                    }
                ],
            }
        )
    )
    assert result == {
        "content": [
            {"type": "text", "text": "Started Fix the build: ahp-chat://node/1\non my-mac-mini"}
        ]
    }
