"""The reference agent hosted through `AcpProvider`, against the packaged loopback stub.

The agent is an ordinary ACP agent, so the provider's permissions, client-tool relay, structured
output and decline-feedback paths apply to it unchanged; these tests prove it end to end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.provider.base import UserMessage
from ahp_host.provider.policy import ToolPolicy
from ahp_protocol.ext.structured import OUTPUT_SCHEMA_META_KEY

from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec
from ahp_host_acp.reference import command
from ahp_host_acp.reference.stub import Reply, StubServer, call, refuse, say

from .fakes import RecordingSink, context
from .hosting import connect, found, open_session, run_turn, shut
from .test_structured_acp import _calls

WAIT = 10.0
PING = {
    "name": "ping",
    "description": "Reply pong",
    "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}},
}


def _provider(root: Path, stub: StubServer, **spec: Any) -> AcpProvider:
    env = stub.agent_env(api_key="sk-test-secret")
    return AcpProvider(root, AgentSpec(command(), env=env, **spec))


async def _session(root: Path, stub: StubServer, **spec: Any) -> AcpSession:
    return await _provider(root, stub, **spec).create_session(
        context(root, active_client_id="c1", client_tools=(PING,))
    )


async def _say(session: AcpSession, text: str, sink: RecordingSink | None = None) -> RecordingSink:
    sink = sink or RecordingSink()
    await asyncio.wait_for(session.send_user_message(UserMessage(text=text), sink), WAIT)
    return sink


def _said(sink: RecordingSink) -> str:
    return "".join(str(e[1]) for e in sink.events if e[0] == "text")


def _failed(sink: RecordingSink) -> list[tuple[Any, ...]]:
    return [e for e in sink.events if e[0] == "failed"]


async def test_refhosted_text_and_reasoning(tmp_path: Path) -> None:
    with StubServer([say("Hello", " there", reasoning=("thinking",))]) as stub:
        session = await _session(tmp_path, stub)
        try:
            sink = await _say(session, "hi")
        finally:
            await session.aclose()
    assert ("reasoning", "thinking") in sink.events
    assert _said(sink) == "Hello there"
    assert _failed(sink) == []


async def test_refhosted_client_tool_round_trip(tmp_path: Path) -> None:
    replies = [call("mcp__ahp_client__ping", {"n": 1}), say("done")]
    with StubServer(replies) as stub:
        session = await _session(tmp_path, stub)
        try:
            sink = await _say(session, "ping it")
        finally:
            await session.aclose()
        assert [(c.name, c.tool_input) for c in sink.client_calls] == [("ping", {"n": 1})]
        # The host answers a relayed call itself, so the user is not asked.
        assert sink.confirmations == []
        assert stub.requests[1]["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "pong",
        }
    assert _said(sink) == "done"


async def test_refhosted_structured_output(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    replies = [call("mcp__ahp_client__StructuredOutput", {"answer": 42}), say("ok")]
    with StubServer(replies) as stub:
        host = Host(_provider(tmp_path, stub), LoopbackSingleUserPolicy())
        wire, serving = await connect(host)
        try:
            chat = await open_session(wire, "acp:/rh1", "acp")
            turn = await run_turn(wire, chat, "t1", "answer", meta={OUTPUT_SCHEMA_META_KEY: schema})
            [result] = _calls(turn)
            assert result["success"] is True
            assert result["structuredContent"] == {"answer": 42}
            assert not found(turn, lambda m: m.get("status") == "pending-confirmation")
        finally:
            await shut(host, wire, serving)


async def test_refhosted_decline_reason_reaches_the_model(tmp_path: Path) -> None:
    write = call("write_file", {"path": "notes.txt", "content": "x"})
    with StubServer([write, say("ok"), say("second")]) as stub:
        session = await _session(tmp_path, stub)
        try:
            declined = RecordingSink(approve=False, reason_message="use the other file")
            await _say(session, "write", declined)
            assert not (tmp_path / "notes.txt").exists()
            assert stub.requests[1]["messages"][-1]["content"] == "The user declined this call."
            await _say(session, "hello")
        finally:
            await session.aclose()
        assert stub.requests[2]["messages"][-1] == {
            "role": "user",
            "content": "hello\n\n[declined: Write notes.txt] use the other file",
        }


async def test_refhosted_policy_denies_with_reason(tmp_path: Path) -> None:
    write = call("write_file", {"path": ".env", "content": "K=1"})
    policy = ToolPolicy(deny_paths=("**/.env",))
    with StubServer([write, say("ok"), say("x")]) as stub:
        session = await _session(tmp_path, stub, policy=policy)
        try:
            sink = await _say(session, "write")
            assert sink.confirmations == []
            assert not (tmp_path / ".env").exists()
            await _say(session, "next")
        finally:
            await session.aclose()
        last = stub.requests[2]["messages"][-1]["content"]
        assert last.startswith("next\n\n[declined: Write .env] denied by policy")


async def test_refhosted_cancel_mid_stream(tmp_path: Path) -> None:
    with StubServer([Reply(deltas=({"content": "a"},), hang=True)]) as stub:
        session = await _session(tmp_path, stub)
        sink = RecordingSink()
        try:
            turn = asyncio.ensure_future(session.send_user_message(UserMessage(text="go"), sink))
            for _ in range(int(WAIT / 0.02)):
                if any(e[0] == "text" for e in sink.events):
                    break
                await asyncio.sleep(0.02)
            assert any(e[0] == "text" for e in sink.events)
            await session.cancel("stop")
            await asyncio.wait_for(turn, WAIT)
        finally:
            await session.aclose()
        assert _failed(sink) == []
        assert await asyncio.to_thread(stub.disconnected.wait, 5)


async def test_refhosted_rate_limited(tmp_path: Path) -> None:
    with StubServer([refuse()]) as stub:
        session = await _session(tmp_path, stub)
        try:
            sink = await _say(session, "hi")
        finally:
            await session.aclose()
    [(_, message, error_type)] = _failed(sink)
    assert error_type == "acp.-32603"
    assert "429" in message
    assert "rate limited" in message
