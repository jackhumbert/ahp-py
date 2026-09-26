"""Annotations channel reducer.

Ported from ``types/channels-annotations/reducer.ts`` (5 actions). The client
copy at ``clients/typescript/src/types/channels-annotations/reducer.ts`` is
byte-identical apart from its two-line "generated, do not edit" header.

**This channel has no prose specification upstream.** At the pinned tag both
``docs/specification/comments-channel.md`` and ``docs/guide/comments.md`` are
zero-byte files, so the TypeScript reducer, ``channels-annotations/actions.ts``
and the ten fixtures (210-219) are the *only* normative source. Anything below
that is not traceable to one of those three is a guess and should be treated as
such.

Three things about this port are load-bearing:

* **Every one of the five actions is client-dispatchable** --
  ``IS_CLIENT_DISPATCHABLE`` in ``types/_generated.py`` marks all of
  ``annotations/{set,updated,removed,entrySet,entryRemoved}`` ``True`` -- and
  four of them locate state by an id the *client* chose. Upstream reads
  ``action.annotation.id`` and ``annotation.entries.findIndex`` unguarded, which
  throws on a malformed payload; here that would be a fault any peer could
  trigger, so every nested read goes through :func:`_obj` / :func:`_seq`. The
  lookups use :func:`~agent_host_protocol.reducers.js.index_of`, which is `===`:
  an id arriving as a dict or a list is compared **by reference** and so matches
  nothing, exactly as upstream, and no ``TypeError`` escapes on the way.
* **An absent id is not a null one.** ``findIndex(t => t.id === action.annotationId)``
  with an absent ``annotationId`` cannot match an annotation whose ``id`` is
  ``null``, and vice versa. Ported as ``item.get("id") == action.get("id")`` it
  matches both -- and since all five actions are client-dispatchable, that lets a
  peer remove or rewrite an annotation it never named.
* **``annotations/updated`` tests ``!== undefined``, so a present null is a
  written value.** Each of ``origin`` / ``resource`` / ``range`` / ``resolved``
  is copied only when the key is *present*, and an explicit null is then stored
  verbatim. This is the one shape where ``session.py``'s ``_with_optional`` would
  be actively wrong: it drops nulls, which would silently turn "re-anchor to
  null" into "leave unchanged".
* **Dispatch order is the state.** Annotations, and entries within an
  annotation, are appended on first sight and replaced *in place* thereafter, so
  a ``*Set`` for a known id never reorders. Actions naming an unknown id are
  no-ops rather than errors (upstream likens this to
  ``changeset/fileRemoved``).

The "every annotation holds at least one entry" invariant is a producer
obligation, not a reducer one: ``annotations/entryRemoved`` will happily empty an
annotation, and upstream says so explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_host_protocol.reducers.js import get, index_of

__all__ = ["annotations_reducer"]


# ─── Small safe accessors ────────────────────────────────────────────────────


def _obj(value: Any) -> Mapping[str, Any]:
    """A wire object read the way JavaScript reads one.

    ``x.y`` on a non-object is ``undefined`` upstream rather than an
    ``AttributeError``. Used for ``action.annotation`` and ``action.entry``,
    both of which come straight off a client-dispatched action.
    """
    return value if isinstance(value, Mapping) else {}


def _seq(value: Any) -> list[Any]:
    """A list view of a wire array, empty for anything that is not one.

    ``state.annotations`` and ``annotation.entries`` are required fields, but a
    replayed or peer-supplied state may be missing either; upstream would throw
    on the ``findIndex``, and this reducer must not.
    """
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return list(value)
    return []


# ─── Annotations Reducer ─────────────────────────────────────────────────────


def annotations_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    action_type = action.get("type")

    if action_type == "annotations/set":
        annotation = action.get("annotation")
        annotations = _seq(state.get("annotations"))
        # An omitted `annotation` is `undefined` upstream, and an array member
        # set to `undefined` serialises as `null` -- which is what appending
        # `None` produces here too.
        index = index_of(annotations, "id", get(annotation, "id"))
        if index < 0:
            return {**state, "annotations": [*annotations, annotation]}
        updated = list(annotations)
        # Full replacement, `entries` included: a producer that wants to keep the
        # existing entries must carry them on the payload.
        updated[index] = annotation
        return {**state, "annotations": updated}

    if action_type == "annotations/updated":
        annotations = _seq(state.get("annotations"))
        index = index_of(annotations, "id", get(action, "annotationId"))
        if index < 0:
            return state
        updated_annotation = {**_obj(annotations[index])}
        # `action.x !== undefined` -- KEY PRESENCE, not nullness (invariant 18).
        # A present null is a value and is stored; only an absent key leaves the
        # current property alone. Do not "simplify" this to `_with_optional`,
        # which deletes on None and would drop the null.
        # `origin` replaced `turnId` in 0.9.0 (session, chat and turn).
        for key in ("origin", "resource", "range", "resolved"):
            if key in action:
                updated_annotation[key] = action[key]
        # `id`, `entries` and `_meta` are deliberately untouched -- replacing
        # those is what `annotations/set` is for.
        updated = list(annotations)
        updated[index] = updated_annotation
        return {**state, "annotations": updated}

    if action_type == "annotations/removed":
        annotations = _seq(state.get("annotations"))
        index = index_of(annotations, "id", get(action, "annotationId"))
        if index < 0:
            return state
        del annotations[index]
        # `annotations` is already a fresh copy, so the splice cannot reach the
        # input state -- which the host's replay log and issued snapshots alias.
        return {**state, "annotations": annotations}

    if action_type == "annotations/entrySet":
        annotations = _seq(state.get("annotations"))
        annotation_index = index_of(annotations, "id", get(action, "annotationId"))
        if annotation_index < 0:
            return state
        annotation = annotations[annotation_index]
        entry = action.get("entry")
        entries = _seq(get(annotation, "entries"))
        entry_index = index_of(entries, "id", get(entry, "id"))
        if entry_index < 0:
            entries.append(entry)
        else:
            entries[entry_index] = entry
        annotations[annotation_index] = {**_obj(annotation), "entries": entries}
        return {**state, "annotations": annotations}

    if action_type == "annotations/entryRemoved":
        annotations = _seq(state.get("annotations"))
        annotation_index = index_of(annotations, "id", get(action, "annotationId"))
        if annotation_index < 0:
            return state
        annotation = annotations[annotation_index]
        entries = _seq(get(annotation, "entries"))
        entry_index = index_of(entries, "id", get(action, "entryId"))
        if entry_index < 0:
            return state
        # Removing the last entry leaves an EMPTY annotation, exactly as upstream
        # does: the one-entry minimum is enforced by producers, which collapse an
        # annotation with `annotations/removed` instead.
        del entries[entry_index]
        annotations[annotation_index] = {**_obj(annotation), "entries": entries}
        return {**state, "annotations": annotations}

    # Unknown action: return the state unchanged. Never raise -- upstream's
    # `softAssertNever` logs and degrades so a peer speaking a newer version of
    # the protocol still converges (fixture 217).
    return state
