"""A session's client tools reach the agent through the MCP relay, against `fake_agent.py`.

The fake agent starts the `ahp_client` server it was given as a real
subprocess and calls it, so the whole path is exercised: `session/new`'s
`mcpServers`, the stdio relay, the loopback bridge, `run_client_tool`, and the
permission policy that lets the calls through.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.provider.base import UserMessage

from ahp_host_acp import permissions
from ahp_host_acp.mcp import McpServer
from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec

from .fake_agent import PERMISSION_OPTIONS
from .fakes import FAKE_AGENT, RecordingSink, context
from .hosting import answer_client_tools, connect, found, open_session, run_turn, shut, text_of

PING = {
    "name": "ping",
    "description": "Reply pong",
    "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}},
}


def _provider(root: Path, log: Path, **spec: Any) -> AcpProvider:
    return AcpProvider(root, AgentSpec(FAKE_AGENT, env={"FAKE_ACP_LOG": str(log)}, **spec))


async def _session(root: Path, log: Path, **spec: Any) -> AcpSession:
    return await _provider(root, log, **spec).create_session(
        context(root, active_client_id="c1", client_tools=(PING,))
    )


async def _say(session: AcpSession, text: str, sink: RecordingSink | None = None) -> RecordingSink:
    sink = sink or RecordingSink()
    await session.send_user_message(UserMessage(text=text), sink)
    return sink


def _said(sink: RecordingSink) -> str:
    return "".join(str(e[1]) for e in sink.events if e[0] == "text")


def _logged(log: Path, method: str) -> list[Any]:
    lines = log.read_text().splitlines() if log.exists() else []
    return [entry["params"] for entry in map(json.loads, lines) if entry["method"] == method]


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "agent.log"


async def test_the_relay_is_the_last_mcp_server(tmp_path: Path, log: Path) -> None:
    configured = McpServer("local", command=(sys.executable, "-m", "server"))
    session = await _session(tmp_path, log, mcp_servers=(configured,))
    try:
        await _say(session, "hello")
    finally:
        await session.aclose()
    (opened,) = _logged(log, "session/new")
    assert [server["name"] for server in opened["mcpServers"]] == ["local", "ahp_client"]
    relay = opened["mcpServers"][-1]
    assert relay["command"] == sys.executable
    assert relay["args"][:3] == ["-m", "ahp_host_acp.relay", "--port"]
    assert "AHP_ACP_RELAY_TOKEN" in [entry["name"] for entry in relay["env"]]


async def test_the_agent_lists_the_clients_tools(tmp_path: Path, log: Path) -> None:
    session = await _session(tmp_path, log)
    try:
        listed = json.loads(_said(await _say(session, "mcp tools")))
    finally:
        await session.aclose()
    assert listed == {"tools": ["ping"], "changed": 0}


async def test_a_relayed_call_runs_in_the_client_without_asking(tmp_path: Path, log: Path) -> None:
    session = await _session(tmp_path, log)
    sink = RecordingSink()
    try:
        await _say(session, 'mcp call ping {"n": 1}', sink)
    finally:
        await session.aclose()
    said = _said(sink)
    assert json.loads(said)["chose"] == "once"
    # The security decision: the client owns the tool, so the user is not asked.
    assert sink.confirmations == []
    assert [(c.name, c.client_id, c.tool_input) for c in sink.client_calls] == [
        ("ping", "c1", {"n": 1})
    ]
    assert "pong" in said
    assert '"isError": false' in said
    # `run_client_tool` shows the call; the agent's own row would duplicate it.
    assert not [e for e in sink.events if e[0] == "started" and e[1] == "mcp_1"]


async def test_a_relayed_call_with_no_allow_once_option_asks_the_user(
    tmp_path: Path, log: Path
) -> None:
    session = await _session(tmp_path, log)
    sink = RecordingSink()
    try:
        await _say(session, "mcp callalways ping {}", sink)
    finally:
        await session.aclose()
    assert len(sink.confirmations) == 1
    assert sink.client_calls == []


async def test_a_change_of_clients_changes_the_listed_tools(tmp_path: Path, log: Path) -> None:
    session = await _session(tmp_path, log)
    try:
        assert json.loads(_said(await _say(session, "mcp tools")))["tools"] == ["ping"]
        await session.active_clients_changed([{"clientId": "c2", "tools": [{"name": "pong2"}]}])
        listed = json.loads(_said(await _say(session, "mcp tools")))
    finally:
        await session.aclose()
    assert listed == {"tools": ["pong2"], "changed": 1}


async def test_a_relayed_call_through_a_host_is_the_clients_own_row(
    tmp_path: Path, log: Path
) -> None:
    host = Host(_provider(tmp_path, log), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(
            wire, "acp:/tools", "acp", activeClient={"clientId": "c1", "tools": [PING]}
        )
        answering = answer_client_tools(wire, chat, "pong")
        try:
            turn = await run_turn(wire, chat, "t1", "mcp call ping {}")
        finally:
            answering.cancel()
        starts = [
            (frame["params"]["action"] or {})
            for frame in wire.frames
            if frame.get("method") == "action"
            and frame["params"]["action"].get("type") == "chat/toolCallStart"
        ]
        assert [a["contributor"]["clientId"] for a in starts] == ["c1"]
        calls = found(turn, lambda m: m.get("kind") == "toolCall")
        assert [call["toolCall"]["toolName"] for call in calls] == ["ping"]
        assert not found(turn, lambda m: m.get("status") == "pending-confirmation")
        assert json.loads(text_of(turn))["chose"] == "once"
    finally:
        await shut(host, wire, serving)


def test_relayed_answer_picks_allow_once() -> None:
    offered = permissions.acp_options(PERMISSION_OPTIONS)
    assert permissions.relayed_answer(offered) == {
        "outcome": {"outcome": "selected", "optionId": "once"}
    }


def test_relayed_answer_never_picks_always_or_reject() -> None:
    offered = permissions.acp_options([PERMISSION_OPTIONS[0], PERMISSION_OPTIONS[2]])
    assert permissions.relayed_answer(offered) is None
    assert permissions.relayed_answer([]) is None


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        # A shell command a model titled after a relayed tool.
        ("execute", {"command": "rm -rf ~"}),
        ("edit", {"n": 1}),
        # MCP-shaped kind, but not the tool's input: an undeclared property...
        ("other", {"command": "rm -rf ~"}),
        ("none", {"n": 1, "command": "x"}),
        # ...or not an object at all.
        ("other", ["rm", "-rf", "~"]),
    ],
)
async def test_a_call_disguised_as_a_relayed_tool_is_asked_about(
    tmp_path: Path, log: Path, kind: str, raw: Any
) -> None:
    # The security check: the title alone never skips the user's approval.
    session = await _session(tmp_path, log)
    sink = RecordingSink()
    try:
        await _say(session, f"mcp disguise {kind} {json.dumps(raw)}", sink)
    finally:
        await session.aclose()
    assert len(sink.confirmations) == 1
    assert sink.client_calls == []


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        ("other", {"n": 1}),
        ("none", {"n": 1}),
        # A wrong value is the tool owner's to refuse, so it still goes through.
        ("other", {"n": "not a number"}),
    ],
)
async def test_a_genuine_relayed_call_shape_is_let_through(
    tmp_path: Path, log: Path, kind: str, raw: dict[str, Any]
) -> None:
    session = await _session(tmp_path, log)
    sink = RecordingSink()
    try:
        await _say(session, f"mcp disguise {kind} {json.dumps(raw)}", sink)
    finally:
        await session.aclose()
    assert sink.confirmations == []
    assert json.loads(_said(sink))["chose"] == "once"
