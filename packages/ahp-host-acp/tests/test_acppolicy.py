"""The host's tool policy in `_request_permission`: advisory, and it wins over the relay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import UserMessage
from ahp_host.provider.policy import Decision, ToolPolicy, ToolRequest

from ahp_host_acp import permissions
from ahp_host_acp.agent import provider_from_options
from ahp_host_acp.config import ConfigError
from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec
from ahp_host_acp.roots import Roots

from .fake_agent import PERMISSION_OPTIONS
from .fakes import FAKE_AGENT, RecordingSink, context

PING = {
    "name": "ping",
    "description": "Reply pong",
    "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}},
}
POLICY = ToolPolicy(deny_paths=("**/.env",), deny_commands=("git push*",))


async def _session(root: Path, policy: ToolPolicy | None = POLICY, **ctx: Any) -> AcpSession:
    spec = AgentSpec(FAKE_AGENT, env={"FAKE_ACP_LOG": str(root / "agent.log")}, policy=policy)
    return await AcpProvider(root, spec).create_session(context(root, **ctx))


async def _ask(session: AcpSession, call: dict[str, Any], sink: RecordingSink) -> str:
    await session.send_user_message(UserMessage(text="permission " + json.dumps(call)), sink)
    return "".join(str(e[1]) for e in sink.events if e[0] == "text")


def test_acppolicy_kinds_pinned(tmp_path: Path) -> None:
    assert permissions.POLICY_KINDS == {
        "read": "read",
        "search": "read",
        "edit": "write",
        "delete": "write",
        "move": "write",
        "execute": "command",
    }
    run = permissions.tool_request(
        {"kind": "execute", "rawInput": {"command": "git", "args": ["push"]}},
        cwd=tmp_path,
        roots=(tmp_path,),
    )
    assert run.kind == "command"
    assert dict(run.input) == {"command": "git push"}
    edit = permissions.tool_request(
        {
            "kind": "edit",
            "locations": [{"path": "/a"}],
            "content": [{"type": "diff", "path": "/b"}],
        },
        cwd=tmp_path,
        roots=(tmp_path,),
    )
    assert edit.input["paths"] == ("/a", "/b")
    assert (
        permissions.tool_request({"kind": "fetch"}, cwd=tmp_path, roots=(tmp_path,)).kind == "tool"
    )


async def test_acppolicy_denied_path_rejected_without_prompt(tmp_path: Path) -> None:
    session, sink = await _session(tmp_path), RecordingSink()
    call = {
        "toolCallId": "p1",
        "title": "Read .env",
        "kind": "read",
        "status": "pending",
        "locations": [{"path": str(tmp_path / ".env")}],
    }
    try:
        assert await _ask(session, call, sink) == "chose no"
    finally:
        await session.aclose()
    assert sink.confirmations == []


async def test_acppolicy_denied_command(tmp_path: Path) -> None:
    session = await _session(tmp_path)
    denied, allowed = RecordingSink(), RecordingSink()
    try:
        push = {"toolCallId": "c1", "kind": "execute", "rawInput": {"command": "git push origin"}}
        assert await _ask(session, push, denied) == "chose no"
        assert denied.confirmations == []
        echo = {"toolCallId": "c2", "kind": "execute", "rawInput": {"command": "echo hi"}}
        assert await _ask(session, echo, allowed) == "chose once"
        assert len(allowed.confirmations) == 1
    finally:
        await session.aclose()


async def test_acppolicy_wins_over_relay(tmp_path: Path) -> None:
    def check(request: ToolRequest) -> Decision | None:
        return Decision("deny", "no tools") if request.kind == "tool" else None

    session = await _session(
        tmp_path, ToolPolicy(check=check), active_client_id="c1", client_tools=(PING,)
    )
    sink = RecordingSink()
    try:
        await session.send_user_message(UserMessage(text='mcp call ping {"n": 1}'), sink)
    finally:
        await session.aclose()
    said = "".join(str(e[1]) for e in sink.events if e[0] == "text")
    assert json.loads(said) == {"chose": "no"}
    assert sink.client_calls == []


def test_acppolicy_reject_answer() -> None:
    assert permissions.rejected_answer(PERMISSION_OPTIONS[:4])["outcome"]["optionId"] == "no"
    only_always = [o for o in PERMISSION_OPTIONS if o["kind"] == "reject_always"]
    assert permissions.rejected_answer(only_always)["outcome"]["optionId"] == "never"
    assert permissions.rejected_answer([PERMISSION_OPTIONS[1]]) == permissions.CANCELLED


def test_acppolicy_config(tmp_path: Path) -> None:
    roots, state = Roots.single(tmp_path), tmp_path
    provider = provider_from_options(
        {"command": ["x"], "policy": {"deny_paths": ["**/.env"]}}, roots, state
    )
    assert provider.spec.policy is not None
    assert provider.spec.policy.deny_paths == ("**/.env",)
    hosted = provider_from_options({"command": ["x"], "policy": {"host_tools": True}}, roots, state)
    assert hosted.spec.client_methods
    with pytest.raises(ConfigError, match="host_tools"):
        provider_from_options({"command": ["x"], "policy": {"host_tools": "yes"}}, roots, state)


async def test_acppolicy_deny_reason_is_shown(tmp_path: Path) -> None:
    session, sink = await _session(tmp_path), RecordingSink()
    call = {
        "toolCallId": "p1",
        "title": "Read .env",
        "kind": "read",
        "status": "pending",
        "locations": [{"path": str(tmp_path / ".env")}],
    }
    try:
        assert await _ask(session, call, sink) == "chose no"
    finally:
        await session.aclose()
    # Under the call's name at once, and in its result when the turn closes it.
    assert "'**/.env'" in sink.invocations["p1"]
    (completed,) = [e for e in sink.events if e[0] == "completed" and e[1] == "p1"]
    assert completed[2] is False
    texts = [part.get("text", "") for part in completed[3]["content"]]
    assert "denied by policy" in texts[0]
    assert "'**/.env'" in texts[0]


@pytest.mark.parametrize("approve", [True, False])
async def test_acppolicy_ask_suppresses_relayed_allow(tmp_path: Path, approve: bool) -> None:
    def check(request: ToolRequest) -> Decision | None:
        return Decision("ask", "ask about tools") if request.kind == "tool" else None

    session = await _session(
        tmp_path, ToolPolicy(check=check), active_client_id="c1", client_tools=(PING,)
    )
    sink = RecordingSink(approve=approve)
    try:
        await session.send_user_message(UserMessage(text='mcp call ping {"n": 1}'), sink)
    finally:
        await session.aclose()
    # Without the policy the relayed tool is allowed once without a prompt
    # (`test_client_tools_acp.py`); an ask puts it to the user instead.
    assert len(sink.confirmations) == 1
    if not approve:
        said = "".join(str(e[1]) for e in sink.events if e[0] == "text")
        assert json.loads(said) == {"chose": "no"}
        assert sink.client_calls == []
