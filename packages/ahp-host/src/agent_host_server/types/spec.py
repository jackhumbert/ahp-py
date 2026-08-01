"""Structural validation for wire values.

Per ADR 0001 there is no parse step, so validation is explicit and targeted: it
checks that required keys are present and well-shaped, and it **never** rewrites,
drops or reorders anything. Unknown keys and unknown union variants are always
accepted -- that is a protocol requirement, not leniency.

Two rules govern everything here:

* An unknown discriminator is *valid* and resolves to the ``Unknown`` arm.
  Upstream's own reducers do the same (``softAssertNever`` warns and returns the
  state unchanged), and the corpora test it.
* A value that is present but of the wrong shape is an *error*. That is what
  makes the round-trip corpus a real gate on our type declarations rather than a
  tautology over ``json.loads``/``json.dumps``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Check",
    "Field",
    "ObjectSpec",
    "ScalarSpec",
    "Spec",
    "UnionSpec",
    "any_value",
    "array_of",
    "is_bool",
    "is_int",
    "is_number",
    "is_object",
    "is_string",
    "one_of",
    "ref",
]

#: A check returns a list of problems; empty means the value is acceptable.
Check = Callable[[Any, str], list[str]]


def _fail(path: str, expected: str, value: Any) -> list[str]:
    return [f"{path}: expected {expected}, got {type(value).__name__}"]


def is_string(value: Any, path: str) -> list[str]:
    return [] if isinstance(value, str) else _fail(path, "string", value)


def is_int(value: Any, path: str) -> list[str]:
    # bool is a subclass of int in Python; the protocol never conflates them.
    if isinstance(value, bool) or not isinstance(value, int):
        return _fail(path, "integer", value)
    return []


def is_number(value: Any, path: str) -> list[str]:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _fail(path, "number", value)
    return []


def is_bool(value: Any, path: str) -> list[str]:
    return [] if isinstance(value, bool) else _fail(path, "boolean", value)


def is_object(value: Any, path: str) -> list[str]:
    return [] if isinstance(value, Mapping) else _fail(path, "object", value)


def any_value(value: Any, path: str) -> list[str]:
    return []


def array_of(item: Check) -> Check:
    def check(value: Any, path: str) -> list[str]:
        if not isinstance(value, Sequence) or isinstance(value, str | bytes):
            return _fail(path, "array", value)
        problems: list[str] = []
        for index, element in enumerate(value):
            problems += item(element, f"{path}[{index}]")
        return problems

    return check


def one_of(*checks: Check) -> Check:
    """Accept a value matching any alternative; report all failures if none do."""

    def check(value: Any, path: str) -> list[str]:
        collected: list[str] = []
        for alternative in checks:
            problems = alternative(value, path)
            if not problems:
                return []
            collected += problems
        return [f"{path}: matched no alternative ({'; '.join(collected)})"]

    return check


def ref(name: str) -> Check:
    """Defer to another spec by name, resolved at call time so specs may recurse."""

    def check(value: Any, path: str) -> list[str]:
        from agent_host_server.types.protocol import SPECS

        spec = SPECS.get(name)
        if spec is None:
            return [f"{path}: no spec registered for {name!r}"]
        return spec.validate(value, path)

    return check


@dataclass(frozen=True)
class Field:
    name: str
    required: bool = False
    check: Check = any_value


@dataclass(frozen=True)
class Spec:
    name: str

    def validate(self, value: Any, path: str = "$") -> list[str]:
        raise NotImplementedError


@dataclass(frozen=True)
class ScalarSpec(Spec):
    """A non-object protocol type, e.g. ``SessionStatus`` (a bitset integer)."""

    check: Check = any_value

    def validate(self, value: Any, path: str = "$") -> list[str]:
        return self.check(value, path)


@dataclass(frozen=True)
class ObjectSpec(Spec):
    fields: tuple[Field, ...] = ()

    def validate(self, value: Any, path: str = "$") -> list[str]:
        if not isinstance(value, Mapping):
            return _fail(path, f"{self.name} object", value)
        problems: list[str] = []
        for f in self.fields:
            if f.name not in value:
                if f.required:
                    problems.append(f"{path}: missing required key {f.name!r}")
                continue
            # An explicit null is only ever acceptable for an optional field --
            # the wire corpus keeps null and absent distinct, so we do not treat
            # `"x": null` as "x absent" for required fields.
            if value[f.name] is None and not f.required:
                continue
            problems += f.check(value[f.name], f"{path}.{f.name}")
        # Unknown keys are deliberately not reported: no upstream schema uses
        # additionalProperties:false, and a host must relay them verbatim.
        return problems


@dataclass(frozen=True)
class UnionSpec(Spec):
    """A discriminated union. Unknown discriminators resolve to ``Unknown``."""

    key: str = "type"
    variants: Mapping[str, Spec] = field(default_factory=dict)
    #: Fields every variant carries, checked even on the Unknown arm.
    common: tuple[Field, ...] = ()

    def variant_of(self, value: Any) -> str | None:
        """The discriminator, or ``None`` when absent/unknown (the Unknown arm)."""
        if not isinstance(value, Mapping):
            return None
        tag = value.get(self.key)
        if not isinstance(tag, str) or tag not in self.variants:
            return None
        return tag

    def validate(self, value: Any, path: str = "$") -> list[str]:
        if not isinstance(value, Mapping):
            return _fail(path, f"{self.name} object", value)
        tag = value.get(self.key)
        if not isinstance(tag, str):
            return [f"{path}: missing or non-string discriminator {self.key!r}"]
        problems = list(ObjectSpec(self.name, self.common).validate(value, path))
        variant = self.variants.get(tag)
        if variant is None:
            # Forward compatibility: a variant from a newer peer is valid and is
            # relayed verbatim. Only the common fields are enforced.
            return problems
        return problems + variant.validate(value, path)
