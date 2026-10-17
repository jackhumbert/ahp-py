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
