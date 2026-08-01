"""JavaScript value semantics, for reducers ported from TypeScript.

The reference reducers are JavaScript, and three of its comparison rules have no
Python equivalent. Each has already produced a real defect in this repository,
so they live here once rather than being re-derived per module.

## `undefined` is not `null`

JSON has one absent-ish value; JavaScript has two, and the reducers distinguish
them. An absent key reads as ``undefined``; an explicit ``null`` is a value.
``obj.get(key)`` returns ``None`` for both, which silently merges them.

That matters in two opposite directions:

* **Reading.** ``x === undefined`` is *false* for an explicit ``null``. Ported
  as ``x is None`` it becomes true, and a peer that sends ``null`` takes a
  branch the reference never takes. (`AGENTS.md` invariant 18.)
* **Writing.** ``{...state, title: action.title}`` is unconditional. When
  ``action.title`` is absent the key becomes ``undefined``, which
  ``JSON.stringify`` **drops**; when it is ``null`` the key is written through
  as ``null``. A port that treats "value is None" as "delete the key" gets the
  second case wrong, and the divergence is invisible to the fixture corpus
  because its comparator normalises ``null`` away on both sides.

:data:`UNDEFINED` gives the missing third state, so both directions can be
written faithfully.

## `===` is not `==`

* ``true === 1`` is **false**; Python's ``True == 1`` is true.
* Two separately-parsed objects or arrays are **never** ``===``, whatever they
  contain; Python compares them structurally.
* ``1 === 1.0`` *is* true -- they are the same JS number -- and Python agrees,
  so that one needs no special handling.

Every id lookup in the reducers is `findIndex(x => x.id === needle)` over
peer-supplied data, so the difference selects which entry gets mutated.

## `Map`/`Set` keys are SameValueZero, and accept anything

Objects and arrays are legal keys and compare by reference. Python raises
``TypeError`` on them. :func:`key_of` produces a hashable stand-in with the
right identity rules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "UNDEFINED",
    "RefKey",
    "assign",
    "get",
    "index_of",
    "key_of",
    "strict_equal",
    "to_string",
]


class _Undefined:
    """The absent-key sentinel. A singleton, compared with ``is``."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        return "UNDEFINED"

    def __bool__(self) -> bool:
        return False


#: JavaScript ``undefined``: a key that is not present at all. Distinct from
#: ``None``, which is an explicit JSON ``null``.
UNDEFINED: Final = _Undefined()


def get(value: Any, key: str) -> Any:
    """``value[key]`` with JS semantics: :data:`UNDEFINED` when there is no key.

    Also returns :data:`UNDEFINED` for a non-mapping, matching JS property
    access on a primitive -- ``"nope".id`` is ``undefined``, not an error.
    """
    if not isinstance(value, Mapping):
        return UNDEFINED
    return value.get(key, UNDEFINED)


def assign(target: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """``{...target, key: value}`` -- including when *value* is ``null``.

    Only :data:`UNDEFINED` removes the key, because only ``undefined`` is
    dropped by ``JSON.stringify``. Mutates *target*, which every call site
    builds fresh.
    """
    if value is UNDEFINED:
        target.pop(key, None)
    else:
        target[key] = value
    return target


def strict_equal(left: Any, right: Any) -> bool:
    """JavaScript ``===``.

    Objects and arrays compare by **reference**, so two structurally identical
    dicts parsed from two frames are not equal -- which is why an upstream
    `findIndex` on an object-valued id always fails and always appends.
    """
    if left is UNDEFINED or right is UNDEFINED:
        return left is right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, Mapping | list | tuple) or isinstance(right, Mapping | list | tuple):
        return left is right
    # `typeof true !== typeof 1`, so a bool is never `===` a number.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, str) != isinstance(right, str):
        return False
    return bool(left == right)


class RefKey:
    """Identity key for a wire value Python refuses to hash.

    A JS ``Map``/``Set`` takes objects and arrays as keys and compares them by
    reference. Python raises ``TypeError``. Wrapping restores both the
    hashability and the reference semantics.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __hash__(self) -> int:
        return id(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RefKey) and other.value is self.value


def key_of(value: Any) -> Any:
    """A hashable stand-in for a value used as a ``Map``/``Set`` key upstream.

    SameValueZero: ``true`` and ``1`` are distinct keys where Python collides
    them, ``undefined`` and ``null`` are distinct keys, and ``1``/``1.0`` stay
    collided because they are the same JS number.
    """
    if value is UNDEFINED:
        return (False, UNDEFINED)
    if value is None or isinstance(value, str | bool | int | float):
        return (isinstance(value, bool), value)
    return RefKey(value)


def to_string(value: Any) -> str:
    """``String(value)`` -- what JS ``+`` does to a non-string operand.

    ``"$ " + undefined`` is ``"$ undefined"``, not an error and not a no-op. It
    is ugly output, but a reducer that silently drops the chunk instead diverges
    from the reference for the same action stream, which is the one thing this
    project promises it does not do.
    """
    if isinstance(value, str):
        return value
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return ",".join(to_string(item) for item in value)
    return "[object Object]"


def index_of(items: Any, key: str, needle: Any) -> int:
    """``items.findIndex(item => item[key] === needle)``; ``-1`` when absent.

    Non-mapping members participate rather than being skipped: reading a
    property off a JS string or number yields ``undefined``, so such a member is
    a live candidate whenever the needle is itself :data:`UNDEFINED`. Only a
    ``null`` member would throw upstream, and invariant 3 forbids us raising.
    """
    if not isinstance(items, list | tuple):
        return -1
    for index, item in enumerate(items):
        if item is None:
            continue
        if strict_equal(get(item, key), needle):
            return index
    return -1
