from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ahp_acp import (
    DELIBERATELY_UNTYPED,
    AgentMessageChunk,
    AgentThoughtChunk,
    AvailableCommandsUpdate,
    CompactionSummaryChunk,
    CompactionUpdate,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    NoticeUpdate,
    PlanUpdate,
    SessionInfoUpdate,
    SessionUpdate,
    ToolCall,
    ToolCallUpdate,
    UnknownUpdate,
    UsageUpdate,
    UserMessageChunk,
    parse_update,
)
from ahp_acp.updates import PARSERS

SCHEMA_DIR = Path(__file__).parent / "fixtures" / "schema"
TEXT = {"type": "text", "text": "hi"}

CASES: list[tuple[dict[str, Any], type[SessionUpdate]]] = [
    ({"sessionUpdate": "user_message_chunk", "content": TEXT, "messageId": "m1"}, UserMessageChunk),
    ({"sessionUpdate": "agent_message_chunk", "content": TEXT}, AgentMessageChunk),
    ({"sessionUpdate": "agent_thought_chunk", "content": TEXT}, AgentThoughtChunk),
    (
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "t1",
            "title": "Read file",
            "name": "read",
            "kind": "read",
            "status": "pending",
            "content": [{"type": "content", "content": TEXT}],
            "locations": [{"path": "/x"}],
            "rawInput": {"path": "/x"},
        },
        ToolCall,
    ),
    (
        {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"},
        ToolCallUpdate,
    ),
    ({"sessionUpdate": "plan", "entries": [{"content": "a"}]}, PlanUpdate),
    (
        {"sessionUpdate": "available_commands_update", "availableCommands": []},
        AvailableCommandsUpdate,
    ),
    ({"sessionUpdate": "current_mode_update", "currentModeId": "code"}, CurrentModeUpdate),
    ({"sessionUpdate": "config_option_update", "configOptions": []}, ConfigOptionUpdate),
    ({"sessionUpdate": "session_info_update", "title": "T", "updatedAt": "now"}, SessionInfoUpdate),
    ({"sessionUpdate": "usage_update", "used": 5, "size": 100, "cost": {"amount": 1}}, UsageUpdate),
    ({"sessionUpdate": "notice", "severity": "info", "title": "Heads up"}, NoticeUpdate),
    (
        {"sessionUpdate": "compaction_update", "compactionId": "c1", "status": "in_progress"},
        CompactionUpdate,
    ),
    (
        {"sessionUpdate": "compaction_summary_chunk", "compactionId": "c1", "content": TEXT},
        CompactionSummaryChunk,
    ),
]


def wrap(update: dict[str, Any]) -> dict[str, Any]:
    return {"sessionId": "s1", "update": update}


@pytest.mark.parametrize(("update", "expected"), CASES, ids=[c[0]["sessionUpdate"] for c in CASES])
def test_every_typed_kind(update: dict[str, Any], expected: type[SessionUpdate]) -> None:
    parsed = parse_update(wrap({**update, "_meta": {"k": 1}}))
    assert type(parsed) is expected
    assert parsed.session_id == "s1"
    assert parsed.raw == {**update, "_meta": {"k": 1}}
    assert parsed.meta == {"k": 1}


def test_cases_cover_every_parser() -> None:
    assert {c[0]["sessionUpdate"] for c in CASES} == set(PARSERS)


def test_fields() -> None:
    call = parse_update(wrap(CASES[3][0]))
    assert isinstance(call, ToolCall)
    assert (call.tool_call_id, call.title, call.name, call.kind, call.status) == (
        "t1",
        "Read file",
        "read",
        "read",
        "pending",
    )
    assert call.raw_input == {"path": "/x"}
    assert call.locations == [{"path": "/x"}]
    chunk = parse_update(wrap(CASES[0][0]))
    assert isinstance(chunk, UserMessageChunk)
    assert (chunk.content, chunk.message_id) == (TEXT, "m1")
    update = parse_update(wrap(CASES[4][0]))
    assert isinstance(update, ToolCallUpdate)
    assert update.title is None
    assert update.content is None
    usage = parse_update(wrap(CASES[10][0]))
    assert isinstance(usage, UsageUpdate)
    assert (usage.used, usage.size, usage.cost) == (5, 100, {"amount": 1})


def test_unknown_kind_is_passed_through() -> None:
    parsed = parse_update(wrap({"sessionUpdate": "_example/new_kind", "x": 1, "_meta": {"m": 2}}))
    assert isinstance(parsed, UnknownUpdate)
    assert parsed.kind == "_example/new_kind"
    assert parsed.raw["x"] == 1
    assert parsed.meta == {"m": 2}
    assert parsed.problem is None


def test_missing_required_field_is_unknown_with_a_problem() -> None:
    parsed = parse_update(wrap({"sessionUpdate": "tool_call", "title": "no id"}))
    assert isinstance(parsed, UnknownUpdate)
    assert parsed.kind == "tool_call"
    assert parsed.problem is not None
    assert "toolCallId" in parsed.problem


@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        "x",
        {},
        {"sessionId": 5, "update": 7},
        {"sessionId": "s", "update": {}},
        {"sessionId": "s", "update": {"sessionUpdate": ["list"]}},
        {"sessionId": "s", "update": {"sessionUpdate": "usage_update", "used": "many", "size": 1}},
        {"sessionId": "s", "update": {"sessionUpdate": "agent_message_chunk", "content": "str"}},
        {"sessionId": "s", "update": {"sessionUpdate": "plan", "entries": "no", "_meta": 3}},
    ],
)
def test_parsing_never_raises(params: Any) -> None:
    assert isinstance(parse_update(params), SessionUpdate)


def _kinds(schema_file: str) -> set[str]:
    schema = json.loads((SCHEMA_DIR / schema_file).read_text())
    kinds: set[str] = set()
    for variant in schema["$defs"]["SessionUpdate"]["oneOf"]:
        kinds.add(variant["properties"]["sessionUpdate"]["const"])
    return kinds


@pytest.mark.parametrize("schema_file", ["schema.json", "schema.unstable.json"])
def test_schema_drift(schema_file: str) -> None:
    """A schema bump that adds a `sessionUpdate` kind fails here until the kind is
    either typed (in `PARSERS`) or listed in `DELIBERATELY_UNTYPED`."""
    undecided = _kinds(schema_file) - set(PARSERS) - set(DELIBERATELY_UNTYPED)
    assert not undecided, f"new session update kinds need a decision: {sorted(undecided)}"


def test_untyped_list_is_not_stale() -> None:
    assert not set(DELIBERATELY_UNTYPED) & set(PARSERS)
    assert set(DELIBERATELY_UNTYPED) <= _kinds("schema.unstable.json")
    assert set(PARSERS) <= _kinds("schema.json")
