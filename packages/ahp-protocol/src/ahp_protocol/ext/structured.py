"""Structured turns: the constants and the wrap rule host and client share.

This is an ahp-py extension ahead of the spec. The upstream request is
``Message.outputSchema`` plus a structured result on a completed turn, recorded
in ahp-host's ``docs/requests.md``. Until upstream answers, a client sets
``Message._meta[OUTPUT_SCHEMA_META_KEY]`` to a JSON Schema object; the host
offers the turn a host-internal tool named :data:`STRUCTURED_OUTPUT_TOOL`
whose input is that schema, and the validated value comes back as the
``structuredContent`` of that tool call's result. A stock peer ignores the
``_meta`` key, so nothing here changes the wire for it.

It lives in the protocol package because host and client must agree on it byte
for byte: the wrap rule below decides what ``structuredContent`` holds, and a
client that unwrapped differently from the host wrapped would hand callers the
wrong value.

:data:`STRUCTURED_OUTPUT_ERROR_TYPE` is an ``ErrorInfo.errorType`` string (that
field's schema is ``string`` with no enum), **not** a JSON-RPC error code.

The wrap rule exists because MCP tool arguments and AHP
``ToolCallResult.structuredContent`` (schema ``type: object``) must both be
objects. A schema whose top level is anything else (an array, a scalar, a list
type like ``["object", "null"]``, or no ``type`` at all) is wrapped as
``{"value": <instance>}``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "OUTPUT_SCHEMA_META_KEY",
    "STRUCTURED_OUTPUT_ERROR_TYPE",
    "STRUCTURED_OUTPUT_TOOL",
    "STRUCTURED_OUTPUT_TOOL_META_KEY",
    "WRAPPED_KEY",
    "needs_wrapping",
    "tool_input_schema",
    "unwrap",
    "wrap",
]

#: Key in ``Message._meta``; the value is a JSON Schema object.
OUTPUT_SCHEMA_META_KEY: Final = "ahp-py/outputSchema"
#: Name of the host-internal tool the turn is offered.
STRUCTURED_OUTPUT_TOOL: Final = "StructuredOutput"
#: Set to ``True`` in the StructuredOutput tool call's ``_meta``, so a client
#: tool that happens to share the name is never mistaken for it.
STRUCTURED_OUTPUT_TOOL_META_KEY: Final = "ahp-py/structuredOutput"
#: ``ErrorInfo.errorType`` of a turn that produced no valid result.
STRUCTURED_OUTPUT_ERROR_TYPE: Final = "ahp-py/structuredOutput"
#: The single property of the wrapper object.
WRAPPED_KEY: Final = "value"

# Keys that make `$ref`s resolvable; they must stay at the wrapper's root.
_ROOT_KEYS: Final = ("$schema", "$id", "$defs", "definitions")


def needs_wrapping(schema: Mapping[str, Any]) -> bool:
    """True unless the schema's ``type`` is exactly ``"object"``."""
    return schema.get("type") != "object"


def tool_input_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The ``inputSchema`` of the StructuredOutput tool for ``schema``.

    The wrapper hoists ``$schema``, ``$id``, ``$defs`` and ``definitions`` to its
    root so ``#/$defs/...`` references still resolve. Copies are shallow:
    callers must not mutate nested values.
    """
    if not needs_wrapping(schema):
        return dict(schema)
    wrapper: dict[str, Any] = {
        "type": "object",
        "properties": {WRAPPED_KEY: {k: v for k, v in schema.items() if k not in _ROOT_KEYS}},
        "required": [WRAPPED_KEY],
        "additionalProperties": False,
    }
    for key in _ROOT_KEYS:
        if key in schema:
            wrapper[key] = schema[key]
    return wrapper


def wrap(value: Any, schema: Mapping[str, Any]) -> dict[str, Any]:
    """The object form of ``value`` (``structuredContent`` / tool arguments)."""
    if needs_wrapping(schema):
        return {WRAPPED_KEY: value}
    if not isinstance(value, Mapping):
        raise TypeError(f"an object schema needs a mapping value, got {type(value).__name__}")
    return dict(value)


def unwrap(structured: Mapping[str, Any], schema: Mapping[str, Any]) -> Any:
    """Inverse of :func:`wrap`."""
    if needs_wrapping(schema):
        return structured.get(WRAPPED_KEY)
    return dict(structured)
