"""Changeset channel reducer.

Ported from ``types/channels-changeset/reducer.ts`` (8 actions) -- the file list
of an ``ahp-changeset:`` channel, the per-file review flag, and the operation
list with its per-operation status machine. Like the session reducer this one is
time-free; it never reads the clock.

Three things about this port are load-bearing:

* **``undefined`` is not ``null``.** Every upstream ``x === undefined`` test is
  ported as a key-presence check, never ``is None``. Three of them decide
  whether a field is replaced or left alone, and ``changeset/filesReviewChanged``
  -- the one client-dispatchable action here -- reaches two of them.
* **An assignment of ``undefined`` deletes.** ``{ ...state, files: action.files }``
  with no ``files`` on the action leaves the key present-but-undefined, which
  ``JSON.stringify`` drops: the state loses its file list entirely rather than
  keeping the old one. Those sites go through :func:`_carry_optional`, which
  deletes rather than writing ``None``. The fixture comparator normalises
  ``null`` away on both sides, so the corpus cannot catch a wrong choice here.
* **An explicit ``null`` is written through, type or no type.** ``operations``,
  ``ChangesetState.error`` and ``ChangesetOperation.error`` are all declared
  optional, but a ``null`` on the action is not ``undefined``, so upstream
  stores it and it survives serialization -- ``{"operations": null}`` is a state
  the reference reducer really does produce (fixture 141 exercises it and only
  passes because the comparator drops null-valued keys). We reproduce the
  divergence rather than tidying it: parity with the reference is the point.

State is host-authoritative but replayed, and actions come from a peer that may
be newer than us (ADR 0001), so both are read defensively: where a malformed
value would make upstream throw, this port degrades into the no-op the
surrounding branch already has. The one such site reachable from *well-formed*
protocol traffic -- because upstream's own ``operations: null`` produces it --
is called out at the ``changeset/operationStatusChanged`` branch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ahp_protocol.reducers.js import get, index_of, key_of, strict_equal

__all__ = ["changeset_reducer"]

# ─── Vocabulary ──────────────────────────────────────────────────────────────
#
# String-valued TypeScript `const enum` members. Spelled out rather than
# imported so a reader can check this file against reducer.ts without a second
# lookup. The two enums coincidentally share the same member value.

_STATUS_ERROR = "error"
_OPERATION_STATUS_ERROR = "error"


# ─── Small safe accessors ────────────────────────────────────────────────────


def _obj(value: Any) -> Mapping[str, Any]:
    """``{ ...value }``'s source read the way JavaScript reads one.

    ``x.y`` on a non-object is ``undefined`` upstream rather than an
    ``AttributeError``, and spreading one yields ``{}`` rather than a
    ``TypeError``.
    """
    return value if isinstance(value, Mapping) else {}


def _seq(value: Any) -> list[Any]:
    """A list view of a wire array, empty for anything that is not one."""
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return list(value)
    return []


def _set_members(value: Any) -> list[Any]:
    """``new Set(value)``'s members, in JavaScript's terms.

    A Set constructor consumes any iterable, and a **string is one** --
    ``new Set('ab')`` is ``{'a', 'b'}``, not ``{'ab'}``. A client that sends
    ``"files": "a"`` where an array was meant therefore marks the file with id
    ``"a"`` reviewed upstream; an array-only read would no-op instead and leave
    the client's optimistic state permanently ahead of the host's.

    DIVERGENCE, deliberate: ``new Set(3)`` throws upstream, where this returns
    no members. Invariant 3 forbids raising, and the surrounding branch already
    treats "no members" as "match nothing".
    """
    if isinstance(value, str):
        return list(value)
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return list(value)
    return []


def _carry_optional(
    target: dict[str, Any], key: str, action: Mapping[str, Any], source: str
) -> dict[str, Any]:
    """``{ ...target, key: action[source] }`` with JavaScript's assignment semantics.

    An **absent** source key is ``undefined`` upstream, and a property set to
    ``undefined`` never reaches the wire -- so the key is deleted here, taking
    any existing value with it. An **explicit null** is a value: it is written
    through verbatim, matching the reference even where the state type says the
    field is optional.

    Mutates *target*, which is a freshly-built copy at every call site.
    """
    if source in action:
        target[key] = action[source]
    else:
        target.pop(key, None)
    return target


# ─── Changeset Reducer ───────────────────────────────────────────────────────


def changeset_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    action_type = action.get("type")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    if action_type == "changeset/statusChanged":
        status = action.get("status")
        if status == _STATUS_ERROR:
            # Carry `error` only when the new status is `Error`, so a recovered
            # changeset is never left holding a stale one.
            return _carry_optional({**state, "status": status}, "error", action, "error")
        next_state = {**state}
        next_state.pop("error", None)
        return _carry_optional(next_state, "status", action, "status")

    # ── Files ────────────────────────────────────────────────────────────────

    if action_type == "changeset/fileSet":
        file = action.get("file")
        files = _seq(state.get("files"))
        # Appending an unknown id keeps the file order stable; a known id is
        # replaced in place. An unusable payload has no id, matches nothing and
        # is appended verbatim -- `action.file.id` is `undefined`, not a throw.
        index = index_of(files, "id", get(file, "id"))
        if index < 0:
            return {**state, "files": [*files, file]}
        updated = list(files)
        updated[index] = file
        return {**state, "files": updated}

    if action_type == "changeset/fileRemoved":
        files = _seq(state.get("files"))
        index = index_of(files, "id", get(action, "fileId"))
        if index < 0:
            return state
        return {**state, "files": [*files[:index], *files[index + 1 :]]}

    if action_type == "changeset/filesReviewChanged":
        # A Python set raises on a dict or a list, and this action is
        # client-dispatchable, so an untrusted id would fault the reducer.
        # `key_of` keeps those hashable without changing which files match.
        ids = {key_of(identifier) for identifier in _set_members(action.get("files"))}
        reviewed = get(action, "reviewed")
        changed = False
        next_files: list[Any] = []
        for file in _seq(state.get("files")):
            # `ids.has(f.id)` is SameValueZero, under which a file with NO `id`
            # is a different key from one carrying an explicit `null` -- and
            # this is the one client-dispatchable changeset action, so a peer
            # sending `"files": [null]` must not sweep every id-less file.
            if key_of(get(file, "id")) not in ids:
                next_files.append(file)
                continue
            if strict_equal(get(file, "reviewed"), reviewed):
                next_files.append(file)
                continue
            changed = True
            next_files.append(_carry_optional(dict(_obj(file)), "reviewed", action, "reviewed"))
        if not changed:
            return state
        return {**state, "files": next_files}

    # ── Bulk content ─────────────────────────────────────────────────────────

    if action_type == "changeset/contentChanged":
        next_state = {**state}
        _carry_optional(next_state, "files", action, "files")
        # `action.operations === undefined`: presence, not nullness. An absent
        # key leaves the existing operation list alone -- full replacement
        # applies to `files` only -- while an explicit null replaces it with
        # null (see the module docstring).
        if "operations" in action:
            next_state["operations"] = action["operations"]
        # Since 0.9.0 the action carries no `error`: a failure is a
        # `changeset/statusChanged`, and any `error` already in state stays.
        return next_state

    # ── Operations ───────────────────────────────────────────────────────────

    if action_type == "changeset/operationsChanged":
        return _carry_optional({**state}, "operations", action, "operations")

    if action_type == "changeset/operationStatusChanged":
        # `state.operations === undefined`: presence, not nullness.
        if "operations" not in state:
            return state
        # DIVERGENCE, deliberate: upstream then calls `.findIndex` on
        # `state.operations`, which THROWS when a previous
        # `changeset/operationsChanged` stored an explicit null there. A reducer
        # that raises costs a `serverSeq` and rejects the action (invariant 7),
        # so a null list is treated as the empty one it is meant to be and the
        # action no-ops.
        operations = _seq(state.get("operations"))
        index = index_of(operations, "id", get(action, "operationId"))
        if index < 0:
            return state
        current = _obj(operations[index])
        status = action.get("status")
        if status == _OPERATION_STATUS_ERROR:
            next_op = _carry_optional({**current, "status": status}, "error", action, "error")
        else:
            # Carry `error` only when the new status is `Error`, so an operation
            # that recovered or started running does not keep a stale one.
            next_op = {**current}
            next_op.pop("error", None)
            _carry_optional(next_op, "status", action, "status")
        updated = list(operations)
        updated[index] = next_op
        return {**state, "operations": updated}

    if action_type == "changeset/cleared":
        if len(_seq(state.get("files"))) == 0:
            return state
        return {**state, "files": []}

    # Unknown action: return the state unchanged. Never raise -- upstream's
    # `softAssertNever` logs and degrades so a peer speaking a newer version of
    # the protocol still converges (fixture 144).
    return state
