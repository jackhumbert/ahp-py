"""Structured turns through the relay, against `fake_agent.py`.

`structured <json array>` has the fake agent call `StructuredOutput` over the
relay once per object until one is accepted, so the host's whole path is
exercised: the per-turn host tool in the relay's list, the permission policy,
`run_client_tool` and the host's validation and retries.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.provider.base import HostTool, ReturnsStructuredOutput, UserMessage
from ahp_protocol.ext.structured import (
    OUTPUT_SCHEMA_META_KEY,
    STRUCTURED_OUTPUT_ERROR_TYPE,
    STRUCTURED_OUTPUT_TOOL,
    STRUCTURED_OUTPUT_TOOL_META_KEY,
)

from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec

from .fakes import FAKE_AGENT, RecordingSink, context
from .hosting import connect, found, open_session, run_turn, shut, text_of

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
}
META = {OUTPUT_SCHEMA_META_KEY: SCHEMA}


def _provider(root: Path) -> AcpProvider:
    return AcpProvider(root, AgentSpec(FAKE_AGENT))


def _calls(turn: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        m["toolCall"]
        for m in found(turn, lambda m: m.get("kind") == "toolCall")
        if m["toolCall"]["toolName"] == STRUCTURED_OUTPUT_TOOL
    ]


def _errors(turn: dict[str, Any]) -> list[dict[str, Any]]:
    return [m["error"] for m in found(turn, lambda m: m.get("kind") == "error")]


async def test_a_valid_result_is_accepted_without_asking(tmp_path: Path) -> None:
    host = Host(_provider(tmp_path), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "acp:/so1", "acp")
        turn = await run_turn(wire, chat, "t1", 'structured [{"answer": 42}]', meta=META)
        [call] = _calls(turn)
        assert call["success"] is True
        assert call["structuredContent"] == {"answer": 42}
        assert call["_meta"][STRUCTURED_OUTPUT_TOOL_META_KEY] is True
        assert _errors(turn) == []
        # The host runs the tool, so the user is not asked.
        assert not found(turn, lambda m: m.get("status") == "pending-confirmation")
        assert len(found(turn, lambda m: m.get("kind") == "toolCall")) == 1
    finally:
        await shut(host, wire, serving)


async def test_a_mismatch_is_retried(tmp_path: Path) -> None:
    host = Host(_provider(tmp_path), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "acp:/so2", "acp")
        turn = await run_turn(
            wire, chat, "t1", 'structured [{"answer": "x"}, {"answer": 7}]', meta=META
        )
        calls = _calls(turn)
        assert len(calls) == 2
        assert calls[-1]["structuredContent"] == {"answer": 7}
        assert _errors(turn) == []
    finally:
        await shut(host, wire, serving)


async def test_exhausted_retries_fail_the_turn(tmp_path: Path) -> None:
    host = Host(_provider(tmp_path), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "acp:/so3", "acp")
        turn = await run_turn(
            wire,
            chat,
            "t1",
            'structured [{"answer": "a"}, {"answer": "b"}, {"answer": "c"}]',
            meta=META,
        )
        assert [e["errorType"] for e in _errors(turn)] == [STRUCTURED_OUTPUT_ERROR_TYPE]
    finally:
        await shut(host, wire, serving)


async def test_host_tools_are_per_turn(tmp_path: Path) -> None:
    host = Host(_provider(tmp_path), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "acp:/so4", "acp")
        await run_turn(wire, chat, "t1", 'structured [{"answer": 1}]', meta=META)
        turn = await run_turn(wire, chat, "t2", "mcp tools")
        assert STRUCTURED_OUTPUT_TOOL not in json.loads(text_of(turn))["tools"]
    finally:
        await shut(host, wire, serving)


async def test_the_session_offers_host_tools_and_a_follow_up_is_not_a_turn(
    tmp_path: Path,
) -> None:
    session = await _provider(tmp_path).create_session(context(tmp_path))
    try:
        assert isinstance(session, ReturnsStructuredOutput)
        assert session.offers_host_tools() is True
        assert isinstance(session, AcpSession)
        tool = HostTool(
            name=STRUCTURED_OUTPUT_TOOL,
            title="Structured output",
            description="Submit the result",
            input_schema=SCHEMA,
        )
        await session.send_user_message(
            UserMessage(text="hello", host_tools=(tool,)), RecordingSink()
        )
        before = session._default.turns
        assert before == 1
        await session.send_user_message(
            UserMessage(text="hello", host_tools=(tool,), continues_turn=True), RecordingSink()
        )
        assert session._default.turns == before
        await session.send_user_message(UserMessage(text="hello"), RecordingSink())
        assert session._default.turns == before + 1
        assert session._default.host_tools == ()
    finally:
        await session.aclose()
