"""Validate an `agent()` result against `opts.schema` (mirrors ahp_host.core.structured)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import jsonschema
import jsonschema.exceptions
import jsonschema.validators
import referencing
import referencing.exceptions

_REF_KEYS = ("$ref", "$dynamicRef")


def _foreign_ref(schema: Mapping[str, Any]) -> str | None:
    """The first `$ref`/`$dynamicRef` that does not point inside the schema."""
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            for key in _REF_KEYS:
                ref = node.get(key)
                if isinstance(ref, str) and not ref.startswith("#"):
                    return ref
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def schema_error(schema: Mapping[str, Any], value: Any) -> str | None:
    """Why `value` does not satisfy `schema`, or None when it does."""
    cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
    try:
        cls.check_schema(dict(schema))
    except jsonschema.exceptions.SchemaError as exc:
        return f"opts.schema is not a valid JSON Schema: {exc.message}"
    ref = _foreign_ref(schema)
    if ref is not None:
        return f"opts.schema may only reference itself ('#...'), not {ref!r}"
    validator = cls(dict(schema), registry=referencing.Registry())
    try:
        errors = sorted(
            validator.iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path]
        )
    except referencing.exceptions.Unresolvable as exc:
        return f"opts.schema has a reference that does not resolve: {exc}"
    if not errors:
        return None
    e = errors[0]
    path = "/".join(str(p) for p in e.absolute_path) or "(root)"
    return f"result does not match opts.schema: {path}: {e.message}"
