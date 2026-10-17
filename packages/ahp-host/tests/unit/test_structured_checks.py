"""Structured turns, the pure parts: schema lookup, validation, JSON extraction."""

from __future__ import annotations

import pytest
from ahp_protocol.ext.structured import OUTPUT_SCHEMA_META_KEY

from ahp_host.core.structured import (
    StructuredOutputFailed,
    last_json_block,
    make_validator,
    output_schema,
    schema_errors,
)
from ahp_host.provider.base import ReturnsStructuredOutput, UserMessage

ANSWER = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
}


def test_output_schema_absent() -> None:
    assert output_schema({}) is None
    assert output_schema({"_meta": {}}) is None
    assert output_schema({"_meta": "x"}) is None


def test_output_schema_mapping_is_copied() -> None:
    schema = {"type": "object"}
    got = output_schema({"_meta": {OUTPUT_SCHEMA_META_KEY: schema}})
    assert got == schema
    assert got is not schema


def test_output_schema_rejects_non_object() -> None:
    with pytest.raises(StructuredOutputFailed):
        output_schema({"_meta": {OUTPUT_SCHEMA_META_KEY: "x"}})


def test_make_validator() -> None:
    with pytest.raises(StructuredOutputFailed) as info:
        make_validator({"type": "nope"})
    assert str(info.value).startswith("The output schema is not a valid JSON Schema: ")
    make_validator(ANSWER)


def test_schema_errors() -> None:
    validator = make_validator(ANSWER)
    assert schema_errors(validator, {"answer": 1}) is None
    assert "answer:" in (schema_errors(validator, {"answer": "x"}) or "")
    assert "(root)" in (schema_errors(validator, {}) or "")


def test_last_fence_wins() -> None:
    text = '```json\n{"a": 1}\n```\nthen\n```json\n{"a": 2}\n```'
    assert last_json_block(text) == (True, {"a": 2})


def test_non_json_fence_is_skipped() -> None:
    text = '```json\n{"a": 1}\n```\nthen\n```python\nprint(1)\n```'
    assert last_json_block(text) == (True, {"a": 1})


def test_bare_outer_object() -> None:
    assert last_json_block('{"a": {"b": 1}} tail') == (True, {"a": {"b": 1}})


def test_bare_last_value() -> None:
    assert last_json_block('{"a": 1} then [1, 2]') == (True, [1, 2])


def test_null_in_fence() -> None:
    assert last_json_block("```json\nnull\n```") == (True, None)


def test_prose() -> None:
    assert last_json_block("plain prose") == (False, None)


def test_capability_protocol_and_message_defaults() -> None:
    class Offers:
        def offers_host_tools(self) -> bool:
            return True

    assert isinstance(Offers(), ReturnsStructuredOutput)
    assert not isinstance(object(), ReturnsStructuredOutput)
    message = UserMessage(text="x")
    assert message.host_tools == ()
    assert message.continues_turn is False
