"""Per-chat agent, reasoning timing and `resultMetadata` shapes (upstream `c301d494`).

All additive and optional, so each shape pins: absent is still valid, a
well-formed value is valid, a malformed one is reported. `duration` is a
non-negative integer where zero is a recorded value.
"""

from __future__ import annotations

from typing import Any, get_type_hints

import pytest

from ahp_protocol.types import SPECS
from ahp_protocol.types.protocol import (
    ChatConfigState,
    ChatMovedAction,
    ChatReasoningAction,
    ChatState,
    ChatSummary,
    CreateChatParams,
    InitializeResult,
    MultipleChatProvidersCapability,
    ReasoningResponsePart,
    SessionChatSummary,
    ToolCallResult,
)


def _problems(name: str, value: Any) -> list[str]:
    return SPECS[name].validate(value)


_SCHEMA = {"type": "object", "properties": {}}
_INIT = {"protocolVersion": "1.1.0", "serverSeq": 1, "snapshots": []}
_CREATE = {"channel": "ahp-session:/s1", "chat": "ahp-chat:/c2"}
_SUMMARY = {"resource": "ahp-chat:/c", "title": "t", "status": 1, "modifiedAt": "x"}
_RESULT = {"success": True, "pastTenseMessage": "done"}


def test_initialize_result_advertises_multiple_chat_providers_as_presence() -> None:
    assert _problems("InitializeResult", _INIT) == []
    assert _problems("InitializeResult", {**_INIT, "multipleChatProviders": {}}) == []
    # Presence, not truthiness: `{}` is the capability, a non-object is malformed.
    assert _problems("InitializeResult", {**_INIT, "multipleChatProviders": True})
    assert _problems("InitializeResult", {**_INIT, "multipleChatProviders": "yes"})


def test_create_chat_params_provider() -> None:
    assert _problems("CreateChatParams", _CREATE) == []
    assert _problems("CreateChatParams", {**_CREATE, "provider": "other-agent"}) == []
    assert _problems("CreateChatParams", {**_CREATE, "provider": 1})


@pytest.mark.parametrize("name", ["ChatSummary", "SessionChatSummary"])
def test_chat_summaries_carry_an_optional_provider(name: str) -> None:
    assert _problems(name, _SUMMARY) == []
    assert _problems(name, {**_SUMMARY, "provider": "other-agent"}) == []
    assert _problems(name, {**_SUMMARY, "provider": 3})
    assert _problems(name, {"title": "t"})


def test_session_chat_summary_status_is_optional() -> None:
    assert _problems("SessionChatSummary", {"resource": "ahp-chat:/c", "title": "t"}) == []


def test_chat_config_state_schema() -> None:
    assert _problems("ChatConfigState", {"values": {}}) == []
    assert _problems("ChatConfigState", {"schema": _SCHEMA, "values": {"a": 1}}) == []
    assert _problems("ChatConfigState", {"schema": "nope", "values": {}})
    assert _problems("ChatConfigState", {"schema": _SCHEMA})


def test_chat_moved_action() -> None:
    assert _problems("ChatMovedAction", {"type": "chat/moved", "session": "ahp-session:/s2"}) == []
    full = {"type": "chat/moved", "session": "ahp-session:/s2", "provider": "p", "schema": _SCHEMA}
    assert _problems("ChatMovedAction", full) == []
    assert _problems("ChatMovedAction", {"type": "chat/moved"})
    assert _problems("ChatMovedAction", {**full, "provider": 1})
    assert _problems("ChatMovedAction", {**full, "schema": "x"})


def test_reasoning_part_and_action_timing() -> None:
    part = {"kind": "reasoning", "id": "r1", "content": "a"}
    action = {"type": "chat/reasoning", "turnId": "t1", "partId": "r1", "content": "b"}
    for name, base in (("ReasoningResponsePart", part), ("ChatReasoningAction", action)):
        assert _problems(name, base) == []
        assert _problems(name, {**base, "startedAt": "2026-10-19T12:00:00.000Z"}) == []
        # Zero is a recorded duration, not "unknown".
        assert _problems(name, {**base, "duration": 0}) == []
        assert _problems(name, {**base, "duration": 1500}) == []
        assert _problems(name, {**base, "duration": -1})
        assert _problems(name, {**base, "duration": 1.5})
        assert _problems(name, {**base, "duration": "5"})
        assert _problems(name, {**base, "startedAt": 5})


def test_tool_call_result_metadata() -> None:
    assert _problems("ToolCallResult", _RESULT) == []
    meta = {"phase": "server-result", "items": ["one", 2, False], "nullable": None}
    assert _problems("ToolCallResult", {**_RESULT, "structuredContent": {"v": 1}}) == []
    assert _problems("ToolCallResult", {**_RESULT, "resultMetadata": meta}) == []
    assert _problems("ToolCallResult", {**_RESULT, "resultMetadata": "x"})
    assert _problems("ToolCallResult", {"success": True})


def _keys(typed_dict: type) -> set[str]:
    # `from __future__ import annotations` hides NotRequired from
    # `__optional_keys__`, so read the resolved hints instead.
    return set(get_type_hints(typed_dict))


def test_typed_dicts_exist_with_the_new_keys() -> None:
    assert "provider" in _keys(CreateChatParams)
    assert "provider" in _keys(ChatState)
    assert "provider" in _keys(ChatSummary)
    assert "provider" in _keys(SessionChatSummary)
    assert "schema" in _keys(ChatConfigState)
    assert "multipleChatProviders" in _keys(InitializeResult)
    assert {"startedAt", "duration"} <= _keys(ReasoningResponsePart)
    assert {"startedAt", "duration"} <= _keys(ChatReasoningAction)
    assert {"provider", "schema", "session"} <= _keys(ChatMovedAction)
    assert {"structuredContent", "resultMetadata"} <= _keys(ToolCallResult)
    assert MultipleChatProvidersCapability.__total__ is False


def test_the_new_specs_accept_the_upstream_round_trip_payloads() -> None:
    from ahp_protocol.conformance.corpus import round_trip_fixtures

    by_name = {f.id.split("-", 1)[0]: f for f in round_trip_fixtures()}
    assert _problems("ChatMovedAction", by_name["084"].input) == []
    assert _problems("ToolCallResult", by_name["085"].input["result"]) == []
    assert _problems("CreateChatParams", by_name["079"].input) == []
    assert _problems("InitializeResult", by_name["080"].input) == []
    assert _problems("SessionChatSummary", by_name["081"].input["summary"]) == []
    chats = by_name["082"].input["chats"]
    assert all(_problems("SessionChatSummary", chat) == [] for chat in chats)
    assert _problems("ChatConfigState", by_name["083"].input["state"]["config"]) == []
