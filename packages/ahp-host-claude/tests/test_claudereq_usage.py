"""Per-request usage reports (`usage_per_message`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import AgentSessionContext, UserMessage
from ahp_protocol.ext.usage import request_usage_meta
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from ahp_host_claude.agent import from_config, parse_agent_options
from ahp_host_claude.provider import ClaudeProvider
from tests.fakes import FakeClient, RecordingSink

MODEL = "claude-opus-5-5"


def _turn() -> list[Any]:
    def request(output: int) -> AssistantMessage:
        return AssistantMessage(
            content=[TextBlock("ok")],
            model=MODEL,
            usage={"input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": output},
        )

    return [
        request(5),
        request(7),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=2,
            session_id="abc",
            usage={"input_tokens": 20, "cache_read_input_tokens": 200, "output_tokens": 12},
        ),
    ]


async def _run(
    tmp_path: Path, *, per_message: bool, turn: list[Any] | None = None
) -> RecordingSink:
    def factory(options: ClaudeAgentOptions) -> FakeClient:
        return FakeClient(options, [turn if turn is not None else _turn()])

    provider = ClaudeProvider(tmp_path, client_factory=factory, usage_per_message=per_message)
    session = await provider.create_session(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude")
    )
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="x"), sink)
    return sink


async def test_claudereq_usage_per_message_reports(tmp_path: Path) -> None:
    sink = await _run(tmp_path, per_message=True)
    off = await _run(tmp_path, per_message=False)
    assert [e for e in sink.events if e[0] == "usage"] == [
        # A request's input is its uncached prompt (plus cache creation); reads stay apart.
        ("usage", 10, 5, 100, MODEL),
        ("usage", 10, 7, 100, MODEL),
        *[e for e in off.events if e[0] == "usage"],
    ]
    assert sink.usage_meta[:2] == [request_usage_meta(), request_usage_meta()]
    assert sink.usage_meta[2] == off.usage_meta[0]


async def test_claudereq_usage_flag_off(tmp_path: Path) -> None:
    sink = await _run(tmp_path, per_message=False)
    assert [e for e in sink.events if e[0] == "usage"] == [("usage", 110, 12, 100, MODEL)]
    assert len(sink.usage_meta) == 1


def test_claudereq_usage_option_parsed(tmp_path: Path) -> None:
    assert parse_agent_options({}).usage_per_message is False
    assert parse_agent_options({"usage_per_message": True}).usage_per_message is True
    assert parse_agent_options({"usage_per_message": False}).usage_per_message is False
    with pytest.raises(ValueError, match="usage_per_message must be true or false"):
        parse_agent_options({"usage_per_message": "yes"})
    assert from_config(roots=tmp_path, usage_per_message=True).usage_per_message is True
    assert from_config(roots=tmp_path).usage_per_message is False
    with pytest.raises(ValueError, match="usage_per_message must be true or false"):
        from_config(roots=tmp_path, usage_per_message="yes")


def _one_response_three_blocks() -> list[Any]:
    """One API response streamed as three AssistantMessages sharing its id and usage."""
    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 5,
        "output_tokens": 9,
    }
    blocks = [
        AssistantMessage(
            content=[TextBlock(text)], model=MODEL, usage=dict(usage), message_id="msg_1"
        )
        for text in ("a", "b", "c")
    ]
    return [
        *blocks,
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="abc",
            usage=dict(usage),
        ),
    ]


async def test_claudereq_usage_one_report_per_message_id(tmp_path: Path) -> None:
    sink = await _run(tmp_path, per_message=True, turn=_one_response_three_blocks())
    requests = [m for m in sink.usage_meta if m == request_usage_meta()]
    assert len(requests) == 1
    usage = [e for e in sink.events if e[0] == "usage"]
    assert usage[0] == ("usage", 15, 9, 300, MODEL)
    assert len(usage) == 2  # the request once, then the turn report


async def test_claudereq_usage_same_id_reports_only_growth(tmp_path: Path) -> None:
    first = {"input_tokens": 10, "cache_read_input_tokens": 300, "output_tokens": 2}
    later = {**first, "output_tokens": 9}
    turn: list[Any] = [
        AssistantMessage(content=[TextBlock("a")], model=MODEL, usage=first, message_id="m"),
        AssistantMessage(content=[TextBlock("b")], model=MODEL, usage=later, message_id="m"),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="abc",
            usage=later,
        ),
    ]
    sink = await _run(tmp_path, per_message=True, turn=turn)
    usage = [e for e in sink.events if e[0] == "usage"]
    assert usage[:2] == [("usage", 10, 2, 300, MODEL), ("usage", None, 7, None, MODEL)]
