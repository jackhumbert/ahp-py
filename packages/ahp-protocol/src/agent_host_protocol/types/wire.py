"""Wire value representation.

Per ADR 0001, protocol values are plain ``dict``/``list``/scalars. There is no
parse-into-objects step: unknown keys, unknown union variants, unknown enum
values and integers beyond int32 all survive by construction, which a host that
is authoritative for state it replays to *newer* clients requires.

Static typing comes from the ``TypedDict`` views in the sibling modules. This
module holds the runtime pieces that are genuinely needed: the ``JsonValue``
alias, discriminator access that never raises, and the two comparison rules the
upstream conformance corpora demand -- which are *opposite* to each other and so
are named to make a mix-up obvious at the call site.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias

JsonValue: TypeAlias = "bool | int | float | str | list[Any] | dict[str, Any] | None"
JsonObject: TypeAlias = dict[str, Any]

__all__ = [
    "JsonObject",
    "JsonValue",
    "coalesce",
    "discriminator",
    "drop_none",
    "json_equal",
    "reduced_equal",
    "wire_equal",
]


def coalesce(value: Any, fallback: Any) -> Any:
    """TypeScript ``??`` -- fall through only on ``None``.

    Python's ``or`` also falls through on ``""``, ``0`` and ``[]``, which
    silently changes reducer behaviour. Every ported ``??`` uses this so the
    sites stay greppable (``AGENTS.md`` invariant 6).
    """
    return fallback if value is None else value


def discriminator(value: Any, key: str = "type") -> str | None:
    """Read a union discriminator without raising on anything.

    Returns ``None`` when *value* is not a mapping or the key is missing or not
    a string, so callers fall through to their ``Unknown`` arm rather than
    crashing on a variant from a newer peer.
    """
    if not isinstance(value, Mapping):
        return None
    tag = value.get(key)
    return tag if isinstance(tag, str) else None


def drop_none(value: Any) -> Any:
    """Recursively remove ``None``-valued mapping entries.

    The reducer corpus writes an absent optional as JSON ``null``; the reference
    harness rewrites ``null`` to ``undefined`` before comparing, and the Go, Rust
    and Swift harnesses strip them. Python's natural encoding is key-absent, so
    we normalise the same way -- but only for the *reducer* corpus. The wire
    corpus keeps ``null`` and absent distinct (see :func:`wire_equal`).
    """
    if isinstance(value, Mapping):
        return {k: drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [drop_none(v) for v in value]
    return value


def json_equal(left: Any, right: Any) -> bool:
    """Type-aware deep equality for JSON values.

    Plain ``==`` is wrong here in two ways that both produce false passes:

    * ``True == 1`` and ``False == 0`` -- the corpora carry real booleans
      (``reviewed``, ``approved``, ``isComplete``) alongside real integers.
    * ``0 == 0.0`` conflates an integer with a float; the corpora carry real
      floats (``safety: 0.0``, ``0.25``, ``1.5``).

    Comparing via ``json.dumps`` is also wrong -- it is key-order sensitive and
    would spuriously fail on ``0`` vs ``0.0``. So: booleans only match booleans,
    and numbers match on numeric value across int/float.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if left.keys() != right.keys():
            return False
        return all(json_equal(left[k], right[k]) for k in left)
    if isinstance(left, Sequence) and not isinstance(left, str | bytes):
        if not isinstance(right, Sequence) or isinstance(right, str | bytes):
            return False
        if len(left) != len(right):
            return False
        return all(json_equal(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, int | float) and isinstance(right, int | float):
        return left == right
    return type(left) is type(right) and bool(left == right)


def reduced_equal(left: Any, right: Any) -> bool:
    """Compare two reducer states: ``null`` and absent are the SAME thing."""
    return json_equal(drop_none(left), drop_none(right))


def wire_equal(left: Any, right: Any) -> bool:
    """Compare two wire payloads: ``null`` and absent are DISTINCT.

    Upstream states the rule directly: "an absent ``origin`` re-encoding as
    ``"origin": null`` is a failure, not a pass"
    (``test-cases/round-trips/KNOWN-FIDELITY-GAPS.md``).
    """
    return json_equal(left, right)
