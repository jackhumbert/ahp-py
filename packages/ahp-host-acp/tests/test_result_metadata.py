"""`ToolCallResult.resultMetadata` from an ACP tool call's raw output (upstream `c301d494`).

When the agent's `rawOutput` is an MCP `CallToolResult` carrying `_meta`, that is the
result's `resultMetadata`. Nothing else is: a raw output that merely has a `_meta`
key but is not shaped like a tool result is the agent's own data, and an absent or
non-object `_meta` gives none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ahp_host_acp.provider import AcpProvider, AgentSpec
from ahp_host_acp.tools import ToolCall

from .fakes import FAKE_AGENT, RecordingSink, context

DEFAULT = "ahp-chat:/1"
APP = {"ui": {"resourceUri": "ui://panel"}}


def _call(**update: Any) -> ToolCall:
    call = ToolCall("c1")
    call.merge(update)
    return call


@pytest.mark.parametrize(
    "raw",
    [
        {"content": [{"type": "text", "text": "x"}], "_meta": APP},
        {"structuredContent": {"n": 1}, "_meta": APP},
        {"isError": False, "_meta": APP},
    ],
)
def test_an_mcp_result_with_meta_is_the_result_metadata(raw: dict[str, Any]) -> None:
    assert _call(rawOutput=raw).result_metadata == APP


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "plain text",
        {"content": [{"type": "text", "text": "x"}]},
        {"_meta": APP},  # not shaped like a tool result
        {"content": [], "_meta": "nope"},
        {"content": [], "_meta": None},
    ],
)
def test_anything_else_gives_none(raw: Any) -> None:
    assert _call(rawOutput=raw).result_metadata is None


def test_a_later_update_without_output_keeps_the_earlier_one() -> None:
    call = _call(rawOutput={"content": [], "_meta": APP})
    call.merge({"status": "completed"})
    assert call.result_metadata == APP


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "log.jsonl"


async def test_the_completed_call_carries_it_to_the_sink(tmp_path: Path) -> None:
    provider = AcpProvider(tmp_path, AgentSpec(FAKE_AGENT, env={}))
    session = await provider.create_session(context(tmp_path))
    try:
        chat = session._chat_for(DEFAULT)
        sink = RecordingSink()
        await session._on_tool_call(
            chat,
            {
                "toolCallId": "m1",
                "title": "lookup",
                "kind": "other",
                "status": "completed",
                "rawInput": {"q": "x"},
                "rawOutput": {"content": [{"type": "text", "text": "ok"}], "_meta": APP},
            },
            sink,
        )
        await session._on_tool_call(
            chat,
            {"toolCallId": "m2", "title": "plain", "kind": "other", "status": "completed"},
            sink,
        )
        assert sink.result_metadata == {"m1": APP, "m2": None}
    finally:
        await session.aclose()
