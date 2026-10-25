"""The structured-turns extension: constants and the wrap rule, exactly."""

from __future__ import annotations

from typing import Any

import pytest

from ahp_protocol.ext import structured as s

OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
}
ARRAY_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"$ref": "#/$defs/n"},
    "$defs": {"n": {"type": "integer"}},
    "$schema": "https://json-schema.org/draft/2020-12/schema",
}


def test_constants() -> None:
    assert s.OUTPUT_SCHEMA_META_KEY == "ahp-py/outputSchema"
    assert s.STRUCTURED_OUTPUT_TOOL == "StructuredOutput"
    assert s.STRUCTURED_OUTPUT_TOOL_META_KEY == "ahp-py/structuredOutput"
    assert s.STRUCTURED_OUTPUT_ERROR_TYPE == "ahp-py/structuredOutput"
    assert s.WRAPPED_KEY == "value"


def test_needs_wrapping() -> None:
    assert s.needs_wrapping(OBJECT_SCHEMA) is False
    assert s.needs_wrapping(ARRAY_SCHEMA) is True
    assert s.needs_wrapping({}) is True
    assert s.needs_wrapping({"type": ["object", "null"]}) is True


def test_tool_input_schema_object_is_unchanged() -> None:
    assert s.tool_input_schema(OBJECT_SCHEMA) == OBJECT_SCHEMA


def test_tool_input_schema_wraps_and_hoists() -> None:
    out = s.tool_input_schema(ARRAY_SCHEMA)
    assert out["$defs"] == {"n": {"type": "integer"}}
    assert out["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert out["properties"]["value"] == {"type": "array", "items": {"$ref": "#/$defs/n"}}
    assert out["required"] == ["value"]
    assert out["additionalProperties"] is False
    assert out["type"] == "object"


def test_wrapper_resolves_hoisted_defs() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(s.tool_input_schema(ARRAY_SCHEMA))
    assert validator.is_valid({"value": [1, 2]})
    assert not validator.is_valid({"value": ["x"]})


def test_wrap_unwrap_round_trip() -> None:
    assert s.wrap({"n": 1}, OBJECT_SCHEMA) == {"n": 1}
    assert s.unwrap({"n": 1}, OBJECT_SCHEMA) == {"n": 1}
    assert s.wrap([1, 2], ARRAY_SCHEMA) == {"value": [1, 2]}
    assert s.unwrap({"value": [1, 2]}, ARRAY_SCHEMA) == [1, 2]


def test_wrap_rejects_non_object_for_object_schema() -> None:
    with pytest.raises(TypeError):
        s.wrap([1], OBJECT_SCHEMA)


def _call(content: Any, **overrides: Any) -> dict[str, Any]:
    call: dict[str, Any] = {
        "toolName": s.STRUCTURED_OUTPUT_TOOL,
        "status": "completed",
        "success": True,
        "_meta": {s.STRUCTURED_OUTPUT_TOOL_META_KEY: True},
        "structuredContent": content,
    }
    call.update(overrides)
    return {"kind": "toolCall", "toolCall": call}


def test_structured_result_takes_the_last_valid_call_unwrapped() -> None:
    turn = {
        "responseParts": [
            _call({"value": [1]}),
            {"kind": "markdown", "content": "x"},
            _call({"value": [2]}),
            _call({"value": [3]}, success=False),
        ]
    }
    assert s.structured_result(turn, ARRAY_SCHEMA) == [2]
    assert s.structured_result({"responseParts": [_call({"n": 4})]}, OBJECT_SCHEMA) == {"n": 4}


@pytest.mark.parametrize(
    "part",
    [
        _call({"n": 1}, toolName="Other"),
        _call({"n": 1}, status="running"),
        _call({"n": 1}, _meta={}),
        _call([1]),
        "not a part",
    ],
)
def test_structured_result_ignores_anything_but_a_marked_completed_call(part: Any) -> None:
    assert s.structured_result({"responseParts": [part]}, OBJECT_SCHEMA) is None


def test_structured_result_without_parts_is_none() -> None:
    assert s.structured_result({}, OBJECT_SCHEMA) is None
    assert s.structured_result({"responseParts": "x"}, OBJECT_SCHEMA) is None
