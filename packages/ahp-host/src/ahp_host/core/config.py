"""Configuration a client may change, and the gate on changing it.

`root/configChanged` is **client-dispatchable**, and VS Code sends it about ten
times per connection. Today every one of them is dropped -- not by design, but
because `RootState.config` is absent and the reducer's `if (!state.config)`
guard discards the action. Publishing a schema turns that accident into a
feature in a single commit, which is why this module exists before the feature
does.

The gate is **the schema itself**, not a policy callback:

* A key that is not in the published schema is rejected, always, whatever any
  policy says. A host that publishes no schema is therefore unchanged -- it
  still accepts nothing, but now on purpose.
* A value whose type does not match its property is rejected. A `boolean`
  property is a boolean; a peer does not get to store a string there and have
  the embedder discover it later.
* Only then is :meth:`Policy.may_set_root_config` asked, per key.

That ordering matters because the first two hold even for an embedder that
supplied a permissive policy, and a permissive policy is the common case for
loopback hosts. `docs/roadmap.md` §6 puts it plainly: never put `mcpServers`,
or anything else that names a program to run, in a config schema.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["RootConfig", "type_matches"]

#: JSON Schema `type` -> what Python calls it. `integer` is absent because
#: `ConfigPropertySchema.type` does not offer it.
_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def type_matches(schema: Any, value: Any) -> bool:
    """Whether *value* is allowed by a `ConfigPropertySchema`.

    Deliberately narrow: `type` and `enum`, and for arrays the element schema
    (`items`) and cardinality (`minItems` / `maxItems`, 1.0.0). This is an
    admission check on an untrusted action, not a JSON Schema implementation
    -- and a partial validator that looks complete is worse than one that says
    what it does. Object `properties` are not descended into.
    """
    if not isinstance(schema, Mapping):
        return False

    declared = schema.get("type")
    expected = _TYPES.get(declared) if isinstance(declared, str) else None
    if expected is None:
        return False
    # `True` is an `int` in Python and is not a number in JSON. Checked first
    # because `isinstance(True, int)` would otherwise let a bool through a
    # `number` property.
    if isinstance(value, bool) != (declared == "boolean"):
        return False
    if not isinstance(value, expected):
        return False

    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        return False
    if isinstance(value, list):
        return _array_matches(schema, value)
    return True


def _array_matches(schema: Mapping[str, Any], value: list[Any]) -> bool:
    """`minItems` / `maxItems` and every element against `items`.

    A bound that is not a non-negative integer is ignored rather than
    enforced: it is the schema's author who got it wrong, not the client, and
    refusing every value would make the property unsettable.
    """
    low, high = _count(schema.get("minItems")), _count(schema.get("maxItems"))
    if low is not None and len(value) < low:
        return False
    if high is not None and len(value) > high:
        return False
    items = schema.get("items")
    return items is None or all(type_matches(items, item) for item in value)


def _count(bound: Any) -> int | None:
    if isinstance(bound, int) and not isinstance(bound, bool) and bound >= 0:
        return bound
    return None


@dataclass(frozen=True)
class RootConfig:
    """A host's `RootState.config`: what a client may configure, and how.

    Supplied by the embedder or not at all. There is no default schema, for the
    same reason there is no default `Policy`: the safe answer depends entirely
    on what the host is wrapping, and guessing it produces a host that looks
    configurable and is not.
    """

    #: `ConfigSchema.properties` -- property id to `ConfigPropertySchema`.
    properties: Mapping[str, Mapping[str, Any]]
    #: Starting values. Keys outside `properties` are dropped rather than
    #: published: a value with no schema is invisible to a client and
    #: unsettable, so publishing it only misleads.
    values: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": {"type": "object", "properties": dict(self.properties)},
            "values": {k: v for k, v in self.values.items() if k in self.properties},
        }

    def rejection(self, key: str, value: Any) -> str | None:
        """Why this key/value may not be stored, or ``None`` to allow it."""
        schema = self.properties.get(key)
        if schema is None:
            return f"{key!r} is not a configurable property"
        if schema.get("readOnly"):
            return f"{key!r} is read-only"
        if not type_matches(schema, value):
            return f"{key!r} does not accept that value"
        return None
