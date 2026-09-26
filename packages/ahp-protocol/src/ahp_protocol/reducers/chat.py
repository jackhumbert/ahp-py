"""Chat channel reducer.

Ported from ``types/channels-chat/reducer.ts`` -- turn lifecycle, the seven-state
tool call machine, pending messages, input requests (elicitations) and the
derived summary status.

Four things about this port are load-bearing and easy to get wrong:

* **``undefined`` is not ``null``.** Upstream's ``x === undefined`` is FALSE for a
  JSON null, so an explicit null takes the *else* branch. Every such test is
  ported as a key-presence check (``"x" in action``), never ``is None``, which
  would conflate an absent key with a null one. ``??`` and truthiness do treat
  the two alike, so those stay ``is None``: check reducer.ts before changing one.
* **The tool call result is flat.** ``ToolCallCompletedState`` spreads
  ``action.result`` onto the tool call itself; ``success`` / ``pastTenseMessage``
  / ``content`` / ``structuredContent`` / ``error`` are siblings of ``status``,
  not a nested ``result`` object.
* **``[]`` is truthy in JavaScript.** Every upstream ``...(x ? { x } : {})``
  whose ``x`` is an array or object is written here as ``if x is not None``.
  Using Python truthiness would silently drop an empty ``content`` or
  ``options`` array that upstream keeps. The fixture corpus does not cover it.
* **The reducer is pure since 0.9.0.** ``modifiedAt`` used to be stamped from
  the wall clock in six places; upstream now derives it from action data -- a
  turn's ``startedAt`` when it starts, ``startedAt + duration`` when it ends
  (:func:`~ahp_protocol.reducers.clock.add_milliseconds_to_timestamp`),
  and leaves it untouched everywhere else.
* **A turn's error is a response part.** ``chat/error`` appends its
  ``ErrorResponsePart`` to the ended turn instead of setting ``Turn.error``, a
  ``chat/responsePart`` can never smuggle one in, and ``chat/turnResume`` reopens
  the latest turn when that part says ``resumable: true``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ahp_protocol.reducers.clock import add_milliseconds_to_timestamp
from ahp_protocol.reducers.js import (
    UNDEFINED,
    assign,
    get,
    index_of,
    index_of_value,
    key_of,
    strict_equal,
    to_string,
    truthy,
)
from ahp_protocol.types.protocol import SessionStatus, session_status_flags
from ahp_protocol.types.wire import coalesce

__all__ = ["chat_reducer"]

# ─── Vocabulary ──────────────────────────────────────────────────────────────
#
# String-valued TypeScript `const enum`s. Spelled out rather than imported so a
# reader can check this file against reducer.ts without a second lookup.

_MARKDOWN = "markdown"
_REASONING = "reasoning"
_TOOL_CALL = "toolCall"
_INPUT_REQUEST = "inputRequest"
_ERROR_PART = "error"

_STREAMING = "streaming"
_PENDING_CONFIRMATION = "pending-confirmation"
_RUNNING = "running"
_AUTH_REQUIRED = "auth-required"
_PENDING_RESULT_CONFIRMATION = "pending-result-confirmation"
_COMPLETED = "completed"
_CANCELLED = "cancelled"

_CONFIRM_NOT_NEEDED = "not-needed"
_CANCEL_SKIPPED = "skipped"
_CANCEL_RESULT_DENIED = "result-denied"

_CONTRIBUTOR_CLIENT = "client"
_CONTRIBUTOR_MCP = "mcp"

_TURN_COMPLETE = "complete"
_TURN_CANCELLED = "cancelled"
_TURN_ERROR = "error"

_PENDING_MESSAGE_STEERING = "steering"

# SessionStatus is a bitset, not an enum. `INPUT_NEEDED` is a combination --
# `InProgress | (1 << 4)` -- so a turn awaiting input is still in progress.
_STATUS_IDLE = SessionStatus.IDLE
_STATUS_ERROR = SessionStatus.ERROR
_STATUS_IN_PROGRESS = SessionStatus.IN_PROGRESS
_STATUS_INPUT_NEEDED = SessionStatus.INPUT_NEEDED
_STATUS_IS_READ = SessionStatus.IS_READ

#: Bitmask covering the mutually-exclusive activity bits (0-4).
_STATUS_ACTIVITY_MASK = SessionStatus.ACTIVITY_MASK

_NON_TERMINAL_BLOCKING = frozenset(
    {_PENDING_CONFIRMATION, _PENDING_RESULT_CONFIRMATION, _AUTH_REQUIRED}
)
_READY_SOURCE_STATES = frozenset({_STREAMING, _RUNNING, _PENDING_CONFIRMATION})
_COMPLETE_SOURCE_STATES = frozenset({_RUNNING, _PENDING_CONFIRMATION, _AUTH_REQUIRED})
_TERMINAL_STATES = frozenset({_COMPLETED, _CANCELLED})


# ─── Small safe accessors ────────────────────────────────────────────────────
#
# Wire values are plain dicts from a peer that may be newer than us (ADR 0001).
# TypeScript would throw on a malformed part; a host that is authoritative for
# replayed state must instead skip it, so every nested read goes through these.


def _mget(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _has_key(value: Any, key: str) -> bool:
    """Whether ``key`` is PRESENT, which is not the same as non-null.

    Upstream's ``x === undefined`` is false for a JSON ``null``: an explicit null
    is a value, an absent key is not. ``_mget(...) is None`` conflates the two,
    so every site that ports a ``=== undefined`` test goes through this instead.
    """
    return isinstance(value, Mapping) and key in value


def _spread_object(value: Any) -> dict[str, Any]:
    """`{ ...value }` for a wire value that should be an object.

    A JS spread copies own enumerable properties: an array **or a string**
    yields index keys -- `{...'ab'}` is `{'0': 'a', '1': 'b'}` -- and any other
    primitive (numbers, booleans, null, undefined) yields `{}`.
    `dict(["ab", "cd"])` would instead produce `{'a': 'b', 'c': 'd'}` -- silent
    corruption -- or raise on `["p", "q"]`.
    """
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        return {str(index): char for index, char in enumerate(value)}
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return {str(index): item for index, item in enumerate(value)}
    return {}


def _status_in(status: Any, states: frozenset[str]) -> bool:
    """Membership that is total over any JSON value, as upstream's ``===`` chain is.

    ``status`` is not trustworthy: ``chat/toolCallComplete`` spreads the client's
    ``result`` straight onto the tool call, so a peer can set it to a dict or a
    list, and ``x in <frozenset>`` raises ``TypeError`` on those.
    """
    return isinstance(status, str) and status in states


def _seq(value: Any) -> list[Any]:
    """A list view of a wire array, empty for anything that is not one."""
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return list(value)
    return []


def _parts(turn: Any) -> list[Any]:
    return _seq(_mget(turn, "responseParts"))


def _status_bits(state: Mapping[str, Any]) -> int:
    """``state.status`` as an integer. JS coerces a missing ``&`` operand to 0."""
    status = state.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return 0
    return status


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


# ─── Tool call helpers ───────────────────────────────────────────────────────


def _nullish(*values: Any) -> Any:
    """``a ?? b ?? …`` over :func:`js.get` reads: the first non-nullish operand,
    else the **last** operand verbatim -- so a trailing explicit null stays
    null and a trailing absence stays :data:`UNDEFINED`.

    ``types.wire.coalesce`` conflates the two flavours because its call sites
    read with ``.get()``; the tool-call literals must not, because a member that
    ends up ``undefined`` is DROPPED from the reference's JSON output where an
    explicit null survives (the js-semantics oracle compares verbatim).
    """
    for value in values[:-1]:
        if value is not UNDEFINED and value is not None:
            return value
    return values[-1]


def _literal(members: dict[str, Any]) -> dict[str, Any]:
    """A JS object literal as ``JSON.stringify`` emits it: ``undefined``-valued
    members do not exist, explicit nulls do.

    Feed members from :func:`js.get`/:func:`_nullish` so absent and null stay
    distinct on the way in -- a ``.get()`` read collapses both to ``None`` and
    would either keep a key the reference drops or drop a null it keeps.
    Dropping at build time rather than at serialisation keeps the reducers'
    no-``UNDEFINED``-in-output invariant, and is observationally identical:
    every later read goes through :func:`js.get`, for which a key that holds
    ``undefined`` and a key that is missing are the same thing.
    """
    return {key: value for key, value in members.items() if value is not UNDEFINED}


def _tc_base(tc: Mapping[str, Any]) -> dict[str, Any]:
    """The common base fields shared by all tool call lifecycle states.

    Upstream builds this object with plain member reads, so a field the source
    state never had is ``undefined`` and vanishes at ``JSON.stringify`` --
    :func:`_literal` reproduces exactly that, while an explicit null in the
    source state survives.
    """
    return _literal(
        {
            "toolCallId": get(tc, "toolCallId"),
            "toolName": get(tc, "toolName"),
            "displayName": get(tc, "displayName"),
            "intention": get(tc, "intention"),
            "contributor": get(tc, "contributor"),
            "_meta": get(tc, "_meta"),
        }
    )


def _tc_base_with_meta(tc: Mapping[str, Any], meta: Any) -> dict[str, Any]:
    """*meta* is a :func:`js.get` read: ``meta ?? tc._meta`` is JS ``??``, which
    falls through on null AND undefined, and the surviving flavour matters."""
    return assign(_tc_base(tc), "_meta", _nullish(meta, get(tc, "_meta")))


def _refine_contributor(current: Any, next_: Any) -> Any:
    """Client execution ownership is established at start and never changes.

    Upstream logs on both rejection paths; the reducer signature here carries no
    logger, so the rejection is silent (behaviourally identical).
    """
    # `if (!next)` -- JS truthiness, wider than null/undefined: an `''`/`0`/
    # `false` contributor is also ignored rather than replacing a real one.
    if not truthy(next_):
        return current
    if _mget(current, "kind") == _CONTRIBUTOR_CLIENT:
        # `next.clientId === current.clientId` -- strict, both peer-supplied.
        if _mget(next_, "kind") == _CONTRIBUTOR_CLIENT and strict_equal(
            get(next_, "clientId"), get(current, "clientId")
        ):
            return next_
        # Ignoring a contributor change for a client tool call.
        return current
    if _mget(next_, "kind") == _CONTRIBUTOR_CLIENT:
        # Ignoring a late client contributor: ownership must be set at start.
        return current
    return next_


def _resolve_selected_option(options: Any, option_id: Any) -> Any:
    """Find a confirmation option by id. Upstream's ``!id``: ``""`` resolves to None."""
    if not option_id or options is None:
        return None
    for option in _seq(options):
        # `o.id === id` -- strict, so `1` never resolves the option a bool
        # points at and an object id resolves only by reference.
        if strict_equal(get(option, "id"), option_id):
            return option
    return None


# ─── Derived status ──────────────────────────────────────────────────────────


def _has_blocking_tool_call(state: Mapping[str, Any]) -> bool:
    """Whether the active turn blocks on something external to the turn."""
    active = state.get("activeTurn")
    if active is None:
        return False
    return any(
        _mget(part, "kind") == _TOOL_CALL
        and _status_in(_mget(_mget(part, "toolCall"), "status"), _NON_TERMINAL_BLOCKING)
        for part in _parts(active)
    )


def _has_open_input_request(state: Mapping[str, Any]) -> bool:
    active = state.get("activeTurn")
    if active is None:
        return False
    # `part.response === undefined`: a part carrying an explicit `"response": null`
    # is RESOLVED upstream. Reading null as open leaves the channel at
    # `input-needed` where the reference has already dropped to `in-progress`.
    return any(
        _mget(part, "kind") == _INPUT_REQUEST and not _has_key(part, "response")
        for part in _parts(active)
    )


def _find_open_input_request_part(
    response_parts: Sequence[Any], request_id: Any
) -> tuple[int, Mapping[str, Any]] | None:
    for index, part in enumerate(response_parts):
        if (
            isinstance(part, Mapping)
            and part.get("kind") == _INPUT_REQUEST
            # `part.response === undefined` again: presence, not nullness.
            and "response" not in part
            # `part.request.id === requestId` -- strict, both peer-supplied.
            and strict_equal(get(part.get("request"), "id"), request_id)
        ):
            return index, part
    return None


def _with_status_flag(status: int, flag: int, set_: bool) -> int:
    """Set or clear one metadata flag.

    Masked to unsigned 32 bits: JavaScript's bitwise operators coerce to a
    *signed* int32, so a status with bit 31 set emits a negative number there.
    The Go, Rust, Kotlin and Swift clients are all unsigned; we follow them.
    """
    return session_status_flags(status | flag if set_ else status & ~flag)


def _summary_status(state: Mapping[str, Any], terminal_status: int | None = None) -> int:
    """Derive the activity bits from live work, preserving orthogonal flags."""
    if terminal_status is not None:
        activity = terminal_status
    elif _has_open_input_request(state) or _has_blocking_tool_call(state):
        activity = _STATUS_INPUT_NEEDED
    elif state.get("activeTurn") is not None:
        activity = _STATUS_IN_PROGRESS
    else:
        activity = _STATUS_IDLE
    # `&` binds tighter than `|` in both languages: (status & ~MASK) | activity.
    return session_status_flags(_status_bits(state) & ~_STATUS_ACTIVITY_MASK | activity)


def _refresh_summary_status(state: Any) -> Any:
    """Recompute ``status`` after a change that feeds :func:`_summary_status`."""
    status = _summary_status(state)
    if status == state.get("status"):
        return state
    return {**state, "status": status}


# ─── Structural updates ──────────────────────────────────────────────────────


def _end_turn(
    state: Mapping[str, Any],
    turn_id: Any,
    turn_state: str,
    duration: Any,
    terminal_status: int | None = None,
    error_part: Any = UNDEFINED,
) -> Any:
    """Finalize the active turn into a completed turn record.

    Non-terminal tool calls are force-cancelled with reason ``skipped``, and an
    error part -- ``chat/error``'s, since 0.9.0 -- is appended last.
    """
    active = state.get("activeTurn")
    # `state.activeTurn.id !== turnId` -- strict: `1` must not end the turn a
    # bool names, and vice versa. `get` keeps absent distinct from null.
    if active is None or not strict_equal(get(active, "id"), turn_id):
        return state

    response_parts: list[Any] = []
    for part in _parts(active):
        if _mget(part, "kind") != _TOOL_CALL:
            response_parts.append(part)
            continue
        tc = _mget(part, "toolCall")
        status = _mget(tc, "status")
        if _status_in(status, _TERMINAL_STATES):
            response_parts.append(part)
            continue
        streaming = status == _STREAMING
        # The replacement part carries only `kind` and `toolCall`: any other key
        # the producer put on the part is dropped, exactly as upstream does.
        response_parts.append(
            {
                "kind": _TOOL_CALL,
                "toolCall": {
                    "status": _CANCELLED,
                    **_tc_base(tc if isinstance(tc, Mapping) else {}),
                    "invocationMessage": (
                        coalesce(_mget(tc, "invocationMessage"), "")
                        if streaming
                        else _mget(tc, "invocationMessage")
                    ),
                    "toolInput": None if streaming else _mget(tc, "toolInput"),
                    "reason": _CANCEL_SKIPPED,
                },
            }
        )
    # `if (errorPart)` -- JavaScript truthiness, so an absent or null part adds
    # nothing and any object is appended verbatim.
    if truthy(error_part):
        response_parts.append(error_part)

    # Defensive clamp, as upstream: the duration is producer-supplied and
    # opaque, but a negative value would be nonsensical to display.
    # `Math.max(0, x)` coerces -- null is 0 -- and NaN JSON-encodes as null.
    clamped = _js_max_zero(duration)
    turn = _literal(
        {
            "id": get(active, "id"),
            "startedAt": get(active, "startedAt"),
            "duration": clamped,
            "message": get(active, "message"),
            "responseParts": response_parts,
            "usage": get(active, "usage"),
            "state": turn_state,
        }
    )

    # `activeTurn: undefined` -- dropped by `JSON.stringify`, so no key at all.
    next_state = {
        **{key: value for key, value in state.items() if key != "activeTurn"},
        "turns": [*_seq(state.get("turns")), turn],
    }
    # `addMillisecondsToTimestamp(active.startedAt, turn.duration ?? 0)`, where
    # `turn.duration` is `Math.max(0, duration)` -- so null reads as 0 and an
    # absent or non-numeric duration is NaN.
    #
    # DIVERGENCE, deliberate: where that throws upstream (an unparseable
    # `startedAt`, a NaN duration), the reference rejects the whole action and
    # the turn stays active forever -- no later action can end it. This port
    # ends the turn and keeps the previous `modifiedAt` instead.
    modified_at = add_milliseconds_to_timestamp(_mget(active, "startedAt"), clamped)
    if modified_at is not None:
        next_state["modifiedAt"] = modified_at
    return {**next_state, "status": _summary_status(next_state, terminal_status)}


def _js_max_zero(value: Any) -> float | None:
    """``Math.max(0, value)``, with ``None`` standing in for NaN."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return max(0, value) if value == value else None
    if isinstance(value, str):
        try:
            number = float(value.strip()) if value.strip() else 0.0
        except ValueError:
            return None
        return max(0, number) if number == number else None
    return None


def _has_resumable_error(turn: Any) -> bool:
    """``turn.responseParts.at(-1)?.kind === 'error' && part.resumable === true``."""
    parts = _mget(turn, "responseParts")
    if not isinstance(parts, list) or not parts:
        return False
    last = parts[-1]
    return _mget(last, "kind") == _ERROR_PART and _mget(last, "resumable") is True


def _upsert_input_request_part(state: Mapping[str, Any], request: Any) -> Any:
    active = state.get("activeTurn")
    if active is None:
        return state
    response_parts = _parts(active)
    existing = _find_open_input_request_part(response_parts, get(request, "id"))
    if existing is not None:
        index, part = existing
        # Answer drafts survive a re-request unless the request carries its own.
        merged = {
            **(request if isinstance(request, Mapping) else {}),
            "answers": coalesce(_mget(request, "answers"), _mget(part.get("request"), "answers")),
        }
        response_parts[index] = {"kind": _INPUT_REQUEST, "request": merged}
    else:
        response_parts.append({"kind": _INPUT_REQUEST, "request": request})

    next_state = {**state, "activeTurn": {**active, "responseParts": response_parts}}
    return {
        **next_state,
        "status": _with_status_flag(_summary_status(next_state), _STATUS_IS_READ, False),
    }


def _update_tool_call_in_parts(
    state: Any,
    turn_id: Any,
    tool_call_id: Any,
    updater: Callable[[Mapping[str, Any]], Any],
) -> Any:
    """Immutably update a tool call inside the active turn's response parts.

    Returns ``state`` unchanged when the turn or tool call does not match, or
    when the updater declines the transition by returning its input unchanged
    (upstream detects that by identity, so this does too).
    """
    active = state.get("activeTurn")
    if active is None or not strict_equal(get(active, "id"), turn_id):
        return state

    found = False
    response_parts: list[Any] = []
    for part in _parts(active):
        tc = _mget(part, "toolCall")
        if (
            _mget(part, "kind") == _TOOL_CALL
            and isinstance(tc, Mapping)
            # `part.toolCall.toolCallId === toolCallId` -- strict, so a bool
            # never selects the call a number owns on peer-controlled ids.
            and strict_equal(get(tc, "toolCallId"), tool_call_id)
        ):
            updated = updater(tc)
            if updated is tc:
                response_parts.append(part)
                continue
            found = True
            response_parts.append({**part, "toolCall": updated})
            continue
        response_parts.append(part)

    if not found:
        return state
    return {**state, "activeTurn": {**active, "responseParts": response_parts}}


def _update_response_part(
    state: Mapping[str, Any],
    turn_id: Any,
    part_id: Any,
    updater: Callable[[Mapping[str, Any]], Any],
) -> Any:
    """Immutably update the first response part matching ``part_id``.

    Tool call parts match on ``toolCall.toolCallId``; everything else on ``id``.
    A part from a newer peer carrying an unrecognised ``kind`` has neither, so it
    is skipped rather than treated as an error (fixture 103).
    """
    active = state.get("activeTurn")
    if active is None or not strict_equal(get(active, "id"), turn_id):
        return state

    found = False
    response_parts: list[Any] = []
    for part in _parts(active):
        if not found and isinstance(part, Mapping):
            if part.get("kind") == _TOOL_CALL:
                identifier = get(part.get("toolCall"), "toolCallId")
            else:
                # `'id' in part ? part.id : undefined` -- presence-aware, so a
                # part without an id only matches an absent `partId`.
                identifier = get(part, "id")
            # `id === partId` -- strict.
            if strict_equal(identifier, part_id):
                found = True
                response_parts.append(updater(part))
                continue
        response_parts.append(part)

    if not found:
        return state
    return {**state, "activeTurn": {**active, "responseParts": response_parts}}


def _append_text(kind: str, content: Any) -> Callable[[Mapping[str, Any]], Any]:
    """Updater for ``chat/delta`` and ``chat/reasoning``: append to a matching part.

    DELIBERATE DIVERGENCE, do not "fix": upstream concatenates with JS ``+``
    (``part.content + action.content``), so a null or numeric ``content``
    appends the literal ``"null"`` / ``"5"``. Ours appends nothing for any
    non-string -- the same stance the ``chat/toolCallDelta`` branch documents
    for ``partialInput``: a null is not text.
    """

    def updater(part: Mapping[str, Any]) -> Any:
        if part.get("kind") == kind:
            return {**part, "content": _as_text(part.get("content")) + _as_text(content)}
        return part

    return updater


# ─── Chat Reducer ────────────────────────────────────────────────────────────


def chat_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    """Pure reducer for chat state. 30 action variants."""
    action_type = action.get("type")

    # ── Turn lifecycle ───────────────────────────────────────────────────────

    if action_type == "chat/turnStarted":
        next_state = {
            **state,
            # `usage: undefined` upstream -- a member `JSON.stringify` drops, so
            # the key must not exist here; the other three keep an explicit
            # null distinct from absence.
            "activeTurn": _literal(
                {
                    "id": get(action, "turnId"),
                    "startedAt": get(action, "startedAt"),
                    "message": get(action, "message"),
                    "responseParts": [],
                }
            ),
        }
        # `modifiedAt: action.startedAt` since 0.9.0 -- an absent one is
        # `undefined`, which drops the key.
        next_state = assign(
            {
                **next_state,
                "status": _with_status_flag(_summary_status(next_state), _STATUS_IS_READ, False),
            },
            "modifiedAt",
            get(action, "startedAt"),
        )

        # If this turn was auto-started from a pending message, remove it.
        queued_message_id = action.get("queuedMessageId")
        if queued_message_id:  # A string in both languages: "" and absent both skip.
            steering = next_state.get("steeringMessage")
            # `next.steeringMessage?.id === action.queuedMessageId` -- strict.
            if strict_equal(get(steering, "id"), queued_message_id):
                next_state = {**next_state, "steeringMessage": None}
            queued = next_state.get("queuedMessages")
            # `if (next.queuedMessages)`: AN EMPTY ARRAY IS TRUTHY in JavaScript,
            # so this must test presence, not Python truthiness. With `[]` the
            # upstream branch runs and rewrites the field to undefined.
            if queued is not None:
                filtered = [
                    m for m in _seq(queued) if not strict_equal(get(m, "id"), queued_message_id)
                ]
                next_state = {
                    **next_state,
                    "queuedMessages": filtered if len(filtered) > 0 else None,
                }
        return next_state

    if action_type == "chat/delta":
        return _update_response_part(
            state,
            get(action, "turnId"),
            get(action, "partId"),
            _append_text(_MARKDOWN, action.get("content")),
        )

    if action_type == "chat/reasoning":
        return _update_response_part(
            state,
            get(action, "turnId"),
            get(action, "partId"),
            _append_text(_REASONING, action.get("content")),
        )

    if action_type == "chat/responsePart":
        active = state.get("activeTurn")
        if active is None or not strict_equal(get(active, "id"), get(action, "turnId")):
            return state
        # An error part is only ever appended by `chat/error`, which also ends
        # the turn; one arriving here is dropped (0.9.0).
        if get(get(action, "part"), "kind") == _ERROR_PART:
            return state
        return {
            **state,
            "activeTurn": {
                **active,
                "responseParts": [*_parts(active), action.get("part")],
            },
        }

    if action_type == "chat/turnComplete":
        return _end_turn(state, get(action, "turnId"), _TURN_COMPLETE, action.get("duration"))

    if action_type == "chat/turnCancelled":
        return _end_turn(state, get(action, "turnId"), _TURN_CANCELLED, action.get("duration"))

    if action_type == "chat/error":
        return _end_turn(
            state,
            get(action, "turnId"),
            _TURN_ERROR,
            action.get("duration"),
            _STATUS_ERROR,
            get(action, "part"),
        )

    if action_type == "chat/turnResume":
        # Client-dispatchable. Reopens the LATEST turn, and only when it ended
        # in an error whose last part says `resumable: true`; its response
        # parts, the error included, carry over into the reopened turn.
        if truthy(state.get("activeTurn")):
            return state
        history = _seq(state.get("turns"))
        turn = history[-1] if history else None
        if (
            not truthy(turn)
            or not strict_equal(get(turn, "id"), get(action, "turnId"))
            or get(turn, "state") != _TURN_ERROR
            or not _has_resumable_error(turn)
        ):
            return state
        next_state = {
            **state,
            "turns": history[:-1],
            "activeTurn": _literal(
                {
                    "id": get(turn, "id"),
                    "startedAt": _nullish(get(turn, "startedAt"), get(state, "modifiedAt")),
                    "message": get(turn, "message"),
                    "responseParts": get(turn, "responseParts"),
                    "usage": get(turn, "usage"),
                }
            ),
        }
        return {
            **next_state,
            "status": _with_status_flag(_summary_status(next_state), _STATUS_IS_READ, False),
        }

    if action_type == "chat/activityChanged":
        return {**state, "activity": action.get("activity")}

    if action_type == "chat/usage":
        active = state.get("activeTurn")
        if active is None or not strict_equal(get(active, "id"), get(action, "turnId")):
            return state
        # `usage: action.usage` is a literal member over the spread: absent
        # overrides with `undefined` and stringify drops the key.
        return {**state, "activeTurn": assign({**active}, "usage", get(action, "usage"))}

    # ── Working directories ──────────────────────────────────────────────────

    if action_type == "chat/workingDirectorySet":
        # `list.includes(action.directory)` -- strict (SameValueZero): `true` is
        # not `1`, and an absent directory is `undefined`, which no parsed array
        # contains -- so upstream appends even past an explicit null entry.
        directory = get(action, "directory")
        listing = _seq(coalesce(state.get("workingDirectories"), []))
        if index_of_value(listing, directory) >= 0:
            return state
        # `JSON.stringify` writes an `undefined` ARRAY ELEMENT as `null` --
        # unlike an object member, which it drops. DOCUMENTED DIVERGENCE: we
        # store that null image eagerly, so a SECOND absent-directory append in
        # the same process appends another null where upstream's `includes`
        # finds the in-memory `undefined` and no-ops. Keeping the sentinel in
        # state instead would leak it to every consumer's `json.dumps`; the
        # divergence is confined to a malformed action (`directory` is
        # required) repeated within one process lifetime, and is pinned by
        # `tests/unit/test_reducer_hazards.py`.
        appended = None if directory is UNDEFINED else directory
        return {**state, "workingDirectories": [*listing, appended]}

    if action_type == "chat/workingDirectoryRemoved":
        listing = state.get("workingDirectories")
        # `if (!list)`: an empty array is truthy upstream, so presence is the
        # test. It falls through to `indexOf === -1` and no-ops either way.
        if listing is None:
            return state
        # `list.indexOf(action.directory)` -- strict, as above.
        items = _seq(listing)
        index = index_of_value(items, get(action, "directory"))
        if index < 0:
            return state
        return {**state, "workingDirectories": [*items[:index], *items[index + 1 :]]}

    # ── Tool call state machine ──────────────────────────────────────────────

    if action_type == "chat/toolCallStart":
        active = state.get("activeTurn")
        if active is None or not strict_equal(get(active, "id"), get(action, "turnId")):
            return state
        return {
            **state,
            "activeTurn": {
                **active,
                "responseParts": [
                    *_parts(active),
                    {
                        "kind": _TOOL_CALL,
                        "toolCall": _literal(
                            {
                                "toolCallId": get(action, "toolCallId"),
                                "toolName": get(action, "toolName"),
                                "displayName": get(action, "displayName"),
                                "intention": get(action, "intention"),
                                "contributor": get(action, "contributor"),
                                "_meta": get(action, "_meta"),
                                "status": _STREAMING,
                            }
                        ),
                    },
                ],
            },
        }

    if action_type == "chat/toolCallDelta":

        def delta_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _STREAMING:
                return tc
            updated = {**tc}
            # `action._meta !== undefined`: an explicit null CLEARS `_meta`, only
            # an absent key leaves it alone. Presence, not nullness.
            if "_meta" in action:
                updated["_meta"] = action.get("_meta")
            content = action.get("content")
            # DELIBERATE DIVERGENCE, do not "fix": upstream tests
            # `action.content !== undefined` and then concatenates, so
            # `content: null` appends the literal string "null" to `partialInput`.
            # Ours appends nothing. Keep it -- a null is not text.
            if content is not None:
                updated["partialInput"] = _as_text(coalesce(tc.get("partialInput"), "")) + _as_text(
                    content
                )
            # A literal member overrides the spread even with `undefined`, and
            # `JSON.stringify` then drops the key -- so when both sides are
            # absent the key must not exist, which is `assign`'s delete arm.
            return assign(
                updated,
                "invocationMessage",
                _nullish(get(action, "invocationMessage"), get(tc, "invocationMessage")),
            )

        return _update_tool_call_in_parts(
            state, get(action, "turnId"), get(action, "toolCallId"), delta_updater
        )

    if action_type == "chat/toolCallReady":

        def ready_updater(tc: Mapping[str, Any]) -> Any:
            status = tc.get("status")
            if not _status_in(status, _READY_SOURCE_STATES):
                return tc
            base = assign(
                _tc_base_with_meta(tc, get(action, "_meta")),
                "contributor",
                _refine_contributor(get(tc, "contributor"), get(action, "contributor")),
            )
            base = assign(
                base, "intention", _nullish(get(action, "intention"), get(tc, "intention"))
            )
            tool_input = _nullish(
                get(action, "toolInput"),
                UNDEFINED if status == _STREAMING else get(tc, "toolInput"),
            )
            confirmed = get(action, "confirmed")
            # `if (action.confirmed)` -- JS truthiness: declared a string enum,
            # but a peer-sent `{}` or `[]` is truthy there and falsy here.
            if truthy(confirmed):
                return _literal(
                    {
                        "status": _RUNNING,
                        **base,
                        "invocationMessage": get(action, "invocationMessage"),
                        "toolInput": tool_input,
                        "confirmed": confirmed,
                    }
                )
            pending = tc if status == _PENDING_CONFIRMATION else None
            options = _nullish(get(action, "options"), get(pending, "options"))
            ready = _literal(
                {
                    "status": _PENDING_CONFIRMATION,
                    **base,
                    "invocationMessage": get(action, "invocationMessage"),
                    "toolInput": tool_input,
                    "confirmationTitle": _nullish(
                        get(action, "confirmationTitle"), get(pending, "confirmationTitle")
                    ),
                    "riskAssessment": _nullish(
                        get(action, "riskAssessment"), get(pending, "riskAssessment")
                    ),
                    "edits": _nullish(get(action, "edits"), get(pending, "edits")),
                    "editable": _nullish(get(action, "editable"), get(pending, "editable")),
                }
            )
            # `...(options ? { options } : {})` -- ToBoolean, and an empty array
            # is truthy in JavaScript (an empty string is not).
            if truthy(options):
                ready["options"] = options
            return ready

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state, get(action, "turnId"), get(action, "toolCallId"), ready_updater
            )
        )

    if action_type == "chat/toolCallConfirmed":

        def confirmed_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _PENDING_CONFIRMATION:
                return tc
            base = _tc_base_with_meta(tc, get(action, "_meta"))
            selected = _resolve_selected_option(tc.get("options"), action.get("selectedOptionId"))
            # `if (action.approved)` -- JS truthiness: `{}`/`[]` approve there
            # and would cancel here under Python's `bool`.
            if truthy(action.get("approved")):
                # Only inline (string) input is replaceable; referenced input is
                # swapped by the host at the resource, not here.
                # `action.editedToolInput !== undefined`: an explicit null is an
                # edit that CLEARS the input, so this is presence, not nullness.
                tool_input = (
                    action.get("editedToolInput")
                    if "editedToolInput" in action and isinstance(tc.get("toolInput"), str)
                    else get(tc, "toolInput")
                )
                resolved = _literal(
                    {
                        "status": _RUNNING,
                        **base,
                        "invocationMessage": get(tc, "invocationMessage"),
                        "toolInput": tool_input,
                        "confirmed": get(action, "confirmed"),
                    }
                )
            else:
                resolved = _literal(
                    {
                        "status": _CANCELLED,
                        **base,
                        "invocationMessage": get(tc, "invocationMessage"),
                        "toolInput": get(tc, "toolInput"),
                        "reason": get(action, "reason"),
                        "reasonMessage": get(action, "reasonMessage"),
                        "userSuggestion": get(action, "userSuggestion"),
                    }
                )
            # `...(selectedOption ? { selectedOption } : {})` -- ToBoolean; a
            # resolved option is an object and objects are always truthy.
            if truthy(selected):
                resolved["selectedOption"] = selected
            return resolved

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state, get(action, "turnId"), get(action, "toolCallId"), confirmed_updater
            )
        )

    if action_type == "chat/toolCallComplete":

        def complete_updater(tc: Mapping[str, Any]) -> Any:
            status = tc.get("status")
            if not _status_in(status, _COMPLETE_SOURCE_STATES):
                return tc
            result = action.get("result")
            # A *successful* completion from `auth-required` is invalid:
            # execution never resumed after the challenge. Ignored as a no-op.
            # `action.result.success` -- JS truthiness, so `{}`/`[]` count as
            # success there where Python's `bool` would let them through.
            if status == _AUTH_REQUIRED and truthy(_mget(result, "success")):
                return tc
            base = _tc_base_with_meta(tc, get(action, "_meta"))
            post_confirmation = status in (_RUNNING, _AUTH_REQUIRED)
            confirmed = get(tc, "confirmed") if post_confirmation else _CONFIRM_NOT_NEEDED
            selected = get(tc, "selectedOption") if post_confirmation else UNDEFINED
            # Content produced before the call paused for auth is the only
            # content the tool ever produced, unless `result` overrides it.
            pre_auth_content = get(tc, "content") if status == _AUTH_REQUIRED else UNDEFINED
            # Cancelling from `auth-required` always completes terminally: the
            # pending challenge is not a "pending result" a client can review,
            # so `requiresResultConfirmation` is ignored on that path.
            # `if (action.requiresResultConfirmation && ...)` -- JS truthiness.
            pending_result = (
                truthy(action.get("requiresResultConfirmation")) and status != _AUTH_REQUIRED
            )
            finished = _literal(
                {
                    "status": _PENDING_RESULT_CONFIRMATION if pending_result else _COMPLETED,
                    **base,
                    "invocationMessage": get(tc, "invocationMessage"),
                    "toolInput": get(tc, "toolInput"),
                    "confirmed": confirmed,
                }
            )
            # Both spreads are `...(x ? { x } : {})` -- ToBoolean. `[]` and `{}`
            # are truthy in JavaScript; `""`, `0` and `false` are not.
            if truthy(selected):
                finished["selectedOption"] = selected
            if truthy(pre_auth_content):
                finished["content"] = pre_auth_content
            # `...action.result` -- the result fields are FLATTENED onto the
            # tool call, not nested, with real JS spread semantics: a string
            # contributes index keys, any other primitive nothing.
            finished.update(_spread_object(result))
            return finished

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state, get(action, "turnId"), get(action, "toolCallId"), complete_updater
            )
        )

    if action_type == "chat/toolCallResultConfirmed":

        def result_confirmed_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _PENDING_RESULT_CONFIRMATION:
                return tc
            base = _tc_base_with_meta(tc, get(action, "_meta"))
            selected = get(tc, "selectedOption")
            # `if (action.approved)` -- JS truthiness, as in toolCallConfirmed.
            if truthy(action.get("approved")):
                reviewed = _literal(
                    {
                        "status": _COMPLETED,
                        **base,
                        "invocationMessage": get(tc, "invocationMessage"),
                        "toolInput": get(tc, "toolInput"),
                        "confirmed": get(tc, "confirmed"),
                        "success": get(tc, "success"),
                        "pastTenseMessage": get(tc, "pastTenseMessage"),
                        "content": get(tc, "content"),
                        "structuredContent": get(tc, "structuredContent"),
                        "error": get(tc, "error"),
                    }
                )
            else:
                # The result is discarded: a denied result never becomes state.
                reviewed = _literal(
                    {
                        "status": _CANCELLED,
                        **base,
                        "invocationMessage": get(tc, "invocationMessage"),
                        "toolInput": get(tc, "toolInput"),
                        "reason": _CANCEL_RESULT_DENIED,
                    }
                )
            # `...(tc.selectedOption ? { selectedOption } : {})` -- ToBoolean.
            if truthy(selected):
                reviewed["selectedOption"] = selected
            return reviewed

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state,
                get(action, "turnId"),
                get(action, "toolCallId"),
                result_confirmed_updater,
            )
        )

    if action_type == "chat/toolCallContentChanged":

        def content_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _RUNNING:
                return tc
            updated = {**tc}
            # `action._meta !== undefined`: an explicit null CLEARS `_meta`.
            if "_meta" in action:
                updated["_meta"] = action.get("_meta")
            # `content: action.content` is a literal member over the spread: an
            # absent action.content overrides with `undefined`, and stringify
            # then drops the key -- deleting whatever content the call had.
            return assign(updated, "content", get(action, "content"))

        return _update_tool_call_in_parts(
            state, get(action, "turnId"), get(action, "toolCallId"), content_updater
        )

    if action_type == "chat/toolCallAuthRequired":

        def auth_required_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _RUNNING:
                return tc
            contributor = tc.get("contributor")
            # Invariant: auth-required only applies to MCP-contributed calls.
            if contributor is None or _mget(contributor, "kind") != _CONTRIBUTOR_MCP:
                return tc
            paused = _literal(
                {
                    "status": _AUTH_REQUIRED,
                    **_tc_base_with_meta(tc, get(action, "_meta")),
                    "contributor": contributor,
                    "invocationMessage": get(tc, "invocationMessage"),
                    "toolInput": get(tc, "toolInput"),
                    "confirmed": get(tc, "confirmed"),
                }
            )
            selected = get(tc, "selectedOption")
            # Both are `...(x ? { x } : {})` -- ToBoolean; `[]`/`{}` are truthy.
            if truthy(selected):
                paused["selectedOption"] = selected
            content = get(tc, "content")
            if truthy(content):
                paused["content"] = content
            return assign(paused, "auth", get(action, "auth"))

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state,
                get(action, "turnId"),
                get(action, "toolCallId"),
                auth_required_updater,
            )
        )

    if action_type == "chat/toolCallAuthResolved":

        def auth_resolved_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _AUTH_REQUIRED:
                return tc
            # `auth` is intentionally not carried over: the challenge is gone.
            resumed = _literal(
                {
                    "status": _RUNNING,
                    **_tc_base_with_meta(tc, get(action, "_meta")),
                    "invocationMessage": get(tc, "invocationMessage"),
                    "toolInput": get(tc, "toolInput"),
                    "confirmed": get(tc, "confirmed"),
                }
            )
            selected = get(tc, "selectedOption")
            # Both are `...(x ? { x } : {})` -- ToBoolean; `[]`/`{}` are truthy.
            if truthy(selected):
                resumed["selectedOption"] = selected
            content = get(tc, "content")
            if truthy(content):
                resumed["content"] = content
            return resumed

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state,
                get(action, "turnId"),
                get(action, "toolCallId"),
                auth_resolved_updater,
            )
        )

    # ── Truncation ───────────────────────────────────────────────────────────

    if action_type == "chat/truncated":
        # `action.turnId === undefined` clears everything -- and ONLY an absent
        # key does. An explicit null falls through to the id search, finds
        # nothing and no-ops. This action is client-dispatchable, so reading null
        # as absent lets any peer whose serializer writes `null` for an omitted
        # option wipe the entire transcript.
        clear_all = "turnId" not in action
        turn_id = action.get("turnId")
        existing_turns = _seq(state.get("turns"))
        if clear_all:
            turns: list[Any] = []
        else:
            # `t.id === action.turnId` -- strict: `true` must not truncate at
            # the turn whose id is `1`, on a client-dispatchable action.
            index = index_of(existing_turns, "id", turn_id)
            if index < 0:
                return state
            turns = existing_turns[: index + 1]
        next_state = {
            **state,
            "turns": turns,
            "activeTurn": None,
        }
        if clear_all:
            # Upstream `delete`s the key: the window is no longer a tail, so
            # there is nothing older left to page.
            next_state.pop("turnsNextCursor", None)
        return {**next_state, "status": _summary_status(next_state)}

    if action_type == "chat/turnsLoaded":
        existing_turns = _seq(state.get("turns"))
        # `new Set(...)` takes any id; a Python set raises on a dict or a list,
        # and a replayed turn id is whatever the peer sent. `key_of` keeps the
        # unhashable ones out of the set without changing which turns match, and
        # `get` keeps an absent id (`undefined` upstream) a distinct key from an
        # explicit null.
        existing_ids = {key_of(get(t, "id")) for t in existing_turns}
        older = [t for t in _seq(action.get("turns")) if key_of(get(t, "id")) not in existing_ids]
        return {
            **state,
            "turns": [*older, *existing_turns],
            "turnsNextCursor": action.get("turnsNextCursor"),
        }

    # ── Session input requests ───────────────────────────────────────────────

    if action_type == "chat/inputRequested":
        return _upsert_input_request_part(state, action.get("request"))

    if action_type == "chat/inputAnswerChanged":
        active = state.get("activeTurn")
        if active is None:
            return state
        response_parts = _parts(active)
        existing = _find_open_input_request_part(response_parts, get(action, "requestId"))
        if existing is None:
            return state
        index, part = existing
        request = part.get("request")
        answers = _spread_object(_mget(request, "answers"))
        # `String(key)` semantics: `answers` is a plain JS object, so any key is
        # coerced to a string rather than raising. A dict or list key would be
        # `TypeError: unhashable` here otherwise, and the action is
        # client-dispatchable. `get` so an ABSENT questionId coerces to
        # `"undefined"`, as upstream, not `"null"`.
        question_id = to_string(get(action, "questionId"))
        # `action.answer === undefined` -- an explicit null is a real answer
        # value upstream and is STORED, not treated as a deletion.
        if "answer" not in action:
            answers.pop(question_id, None)
        else:
            answers[question_id] = action["answer"]
        # `answers: Object.keys(answers).length > 0 ? answers : undefined` --
        # the explicit `undefined` member OVERWRITES the spread copy and is then
        # dropped by `JSON.stringify`, so deleting the last answer removes the
        # key. Writing None instead would serialize `"answers": null`, which is
        # schema-invalid and `!== undefined` in a reference peer.
        response_parts[index] = {
            **part,
            "request": assign(
                dict(request) if isinstance(request, Mapping) else {},
                "answers",
                answers if len(answers) > 0 else UNDEFINED,
            ),
        }
        return {**state, "activeTurn": {**active, "responseParts": response_parts}}

    if action_type == "chat/inputCompleted":
        active = state.get("activeTurn")
        if active is None:
            return state
        response_parts = _parts(active)
        existing = _find_open_input_request_part(response_parts, get(action, "requestId"))
        if existing is None:
            return state
        index, part = existing
        request = part.get("request")
        # `{ ...(part.request.answers ?? {}), ...(action.answers ?? {}) }` is
        # total over ANY JSON value: a string spreads to index keys, a number to
        # `{}`. Both operands are peer-controlled, so this goes through
        # `_spread_object` -- exactly as `chat/inputAnswerChanged` does -- where
        # a bare `{**...}` would raise on a truthy non-mapping.
        final_answers = {
            **_spread_object(_mget(request, "answers")),
            **_spread_object(action.get("answers")),
        }
        # Same `... : undefined` overwrite-then-drop as `chat/inputAnswerChanged`
        # above: no surviving answers means NO key, never `"answers": null`.
        completed = {
            **part,
            "request": assign(
                dict(request) if isinstance(request, Mapping) else {},
                "answers",
                final_answers if len(final_answers) > 0 else UNDEFINED,
            ),
        }
        # `response: action.response` upstream sets the property to `undefined`
        # when the action omits it, which never reaches the wire. Writing None
        # here would serialize as `"response": null` -- and a reference peer reads
        # that as RESOLVED while we still treat it as open, so the two compute a
        # different `status` for the same channel. Write the key only if it came.
        if "response" in action:
            completed["response"] = action["response"]
        response_parts[index] = completed
        next_state = {**state, "activeTurn": {**active, "responseParts": response_parts}}
        return {**next_state, "status": _summary_status(next_state)}

    # ── Pending messages ─────────────────────────────────────────────────────

    if action_type == "chat/pendingMessageSet":
        entry = {"id": action.get("id"), "message": action.get("message")}
        if action.get("kind") == _PENDING_MESSAGE_STEERING:
            return {**state, "steeringMessage": entry}
        existing_queue = _seq(coalesce(state.get("queuedMessages"), []))
        # `m.id === action.id` -- strict, and `get` keeps an absent action id
        # (`undefined`) matching only a message whose own id is absent.
        index = index_of(existing_queue, "id", get(action, "id"))
        if index >= 0:
            updated_queue = list(existing_queue)
            updated_queue[index] = entry
            return {**state, "queuedMessages": updated_queue}
        return {**state, "queuedMessages": [*existing_queue, entry]}

    if action_type == "chat/pendingMessageRemoved":
        # `... .id !== action.id` (both branches) -- strict: `1` never removes
        # the message `true` names, and an object id removes only by reference.
        target = get(action, "id")
        if action.get("kind") == _PENDING_MESSAGE_STEERING:
            steering = state.get("steeringMessage")
            if steering is None or not strict_equal(get(steering, "id"), target):
                return state
            return {**state, "steeringMessage": None}
        queued = state.get("queuedMessages")
        if queued is None:
            return state
        items = _seq(queued)
        filtered = [m for m in items if not strict_equal(get(m, "id"), target)]
        if len(filtered) == len(items):
            return state
        return {**state, "queuedMessages": filtered if len(filtered) > 0 else None}

    if action_type == "chat/queuedMessagesReordered":
        queued = state.get("queuedMessages")
        # `if (!existing)` -- an empty array is truthy upstream and falls through
        # to produce an (identical) empty queue, so test presence.
        if queued is None:
            return state
        items = _seq(queued)
        # `chat/pendingMessageSet` stores `id` verbatim, so a queued id can be a
        # dict or a list -- fine as a JS Map/Set key, fatal as a Python one.
        # `key_of` wraps those; the ordering below is otherwise untouched.
        by_id: dict[Any, Any] = {}
        for message in items:
            by_id[key_of(get(message, "id"))] = message  # A JS Map: last write wins.
        ordered: set[Any] = set()
        reordered: list[Any] = []
        for identifier in _seq(action.get("order")):
            key = key_of(identifier)
            if key in by_id and key not in ordered:
                ordered.add(key)
                reordered.append(by_id[key])
        # Append anything not mentioned in `order`, preserving the original
        # order, so a client with a stale view never silently drops a message.
        for message in items:
            if key_of(get(message, "id")) not in ordered:
                reordered.append(message)
        return {**state, "queuedMessages": reordered}

    # ── Draft ────────────────────────────────────────────────────────────────

    if action_type == "chat/draftChanged":
        return {**state, "draft": action.get("draft")}

    # Unknown action: return the state unchanged. Never raise -- upstream's
    # `softAssertNever` logs and degrades so a peer speaking a newer version of
    # the protocol still converges. There is no fixture for this on chat.
    return state
