"""Chat channel reducer.

Ported from ``types/channels-chat/reducer.ts`` -- turn lifecycle, the seven-state
tool call machine, pending messages, input requests (elicitations) and the
derived summary status.

Three things about this port are load-bearing and easy to get wrong:

* **The tool call result is flat.** ``ToolCallCompletedState`` spreads
  ``action.result`` onto the tool call itself; ``success`` / ``pastTenseMessage``
  / ``content`` / ``structuredContent`` / ``error`` are siblings of ``status``,
  not a nested ``result`` object.
* **``[]`` is truthy in JavaScript.** Every upstream ``...(x ? { x } : {})``
  whose ``x`` is an array or object is written here as ``if x is not None``.
  Using Python truthiness would silently drop an empty ``content`` or
  ``options`` array that upstream keeps. The fixture corpus does not cover it.
* **The reducer is not pure.** ``modifiedAt`` is stamped from the wall clock in
  six places (upstream lines 218, 253, 359, 723, 779, 812), read only through
  :mod:`agent_host_server.reducers.clock`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent_host_server.reducers.clock import now_iso
from agent_host_server.types.protocol import session_status_flags
from agent_host_server.types.wire import coalesce

__all__ = ["chat_reducer"]

# ─── Vocabulary ──────────────────────────────────────────────────────────────
#
# String-valued TypeScript `const enum`s. Spelled out rather than imported so a
# reader can check this file against reducer.ts without a second lookup.

_MARKDOWN = "markdown"
_REASONING = "reasoning"
_TOOL_CALL = "toolCall"
_INPUT_REQUEST = "inputRequest"

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

# SessionStatus is a bitset, not an enum. Values from
# `types/channels-session/state.ts`. Deliberately re-declared here: the named
# constants on `types.protocol.SessionStatus` are rotated by one position
# relative to upstream (IN_PROGRESS = 1 there, Idle = 1 upstream), so binding to
# them would encode that discrepancy into the reducer.
_STATUS_IDLE = 1
_STATUS_ERROR = 1 << 1
_STATUS_IN_PROGRESS = 1 << 3
_STATUS_INPUT_NEEDED = (1 << 3) | (1 << 4)
_STATUS_IS_READ = 1 << 5

#: Bitmask covering the mutually-exclusive activity bits (0-4).
_STATUS_ACTIVITY_MASK = (1 << 5) - 1

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


def _tc_base(tc: Mapping[str, Any]) -> dict[str, Any]:
    """The common base fields shared by all tool call lifecycle states.

    Every key is emitted even when absent: upstream builds this object with
    explicit ``undefined`` members, and the corpus compares ``null`` and absent
    as the same thing.
    """
    return {
        "toolCallId": tc.get("toolCallId"),
        "toolName": tc.get("toolName"),
        "displayName": tc.get("displayName"),
        "intention": tc.get("intention"),
        "contributor": tc.get("contributor"),
        "_meta": tc.get("_meta"),
    }


def _tc_base_with_meta(tc: Mapping[str, Any], meta: Any) -> dict[str, Any]:
    base = _tc_base(tc)
    base["_meta"] = coalesce(meta, tc.get("_meta"))
    return base


def _refine_contributor(current: Any, next_: Any) -> Any:
    """Client execution ownership is established at start and never changes.

    Upstream logs on both rejection paths; the reducer signature here carries no
    logger, so the rejection is silent (behaviourally identical).
    """
    if next_ is None:
        return current
    if _mget(current, "kind") == _CONTRIBUTOR_CLIENT:
        if _mget(next_, "kind") == _CONTRIBUTOR_CLIENT and _mget(next_, "clientId") == _mget(
            current, "clientId"
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
        if _mget(option, "id") == option_id:
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
        and _mget(_mget(part, "toolCall"), "status") in _NON_TERMINAL_BLOCKING
        for part in _parts(active)
    )


def _has_open_input_request(state: Mapping[str, Any]) -> bool:
    active = state.get("activeTurn")
    if active is None:
        return False
    return any(
        _mget(part, "kind") == _INPUT_REQUEST and _mget(part, "response") is None
        for part in _parts(active)
    )


def _find_open_input_request_part(
    response_parts: Sequence[Any], request_id: Any
) -> tuple[int, Mapping[str, Any]] | None:
    for index, part in enumerate(response_parts):
        if (
            isinstance(part, Mapping)
            and part.get("kind") == _INPUT_REQUEST
            and part.get("response") is None
            and _mget(part.get("request"), "id") == request_id
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
    error: Any = None,
) -> Any:
    """Finalize the active turn into a completed turn record.

    Non-terminal tool calls are force-cancelled with reason ``skipped``.
    """
    active = state.get("activeTurn")
    if active is None or _mget(active, "id") != turn_id:
        return state

    response_parts: list[Any] = []
    for part in _parts(active):
        if _mget(part, "kind") != _TOOL_CALL:
            response_parts.append(part)
            continue
        tc = _mget(part, "toolCall")
        status = _mget(tc, "status")
        if status in _TERMINAL_STATES:
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

    turn = {
        "id": _mget(active, "id"),
        "startedAt": _mget(active, "startedAt"),
        # Defensive clamp, as upstream: the duration is producer-supplied and
        # opaque, but a negative value would be nonsensical to display.
        # `Math.max(0, undefined)` is NaN in JS, which JSON-encodes as null and
        # the corpus treats as absent -- so a missing duration stays absent.
        "duration": max(0, duration) if isinstance(duration, int | float) else None,
        "message": _mget(active, "message"),
        "responseParts": response_parts,
        "usage": _mget(active, "usage"),
        "state": turn_state,
        "error": error,
    }

    next_state = {
        **state,
        "turns": [*_seq(state.get("turns")), turn],
        "activeTurn": None,
        "modifiedAt": now_iso(),
    }
    return {**next_state, "status": _summary_status(next_state, terminal_status)}


def _upsert_input_request_part(state: Mapping[str, Any], request: Any) -> Any:
    active = state.get("activeTurn")
    if active is None:
        return state
    response_parts = _parts(active)
    existing = _find_open_input_request_part(response_parts, _mget(request, "id"))
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
        "modifiedAt": now_iso(),
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
    if active is None or _mget(active, "id") != turn_id:
        return state

    found = False
    response_parts: list[Any] = []
    for part in _parts(active):
        tc = _mget(part, "toolCall")
        if (
            _mget(part, "kind") == _TOOL_CALL
            and isinstance(tc, Mapping)
            and tc.get("toolCallId") == tool_call_id
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
    if active is None or _mget(active, "id") != turn_id:
        return state

    found = False
    response_parts: list[Any] = []
    for part in _parts(active):
        if not found and isinstance(part, Mapping):
            if part.get("kind") == _TOOL_CALL:
                identifier = _mget(part.get("toolCall"), "toolCallId")
            else:
                identifier = part.get("id")
            if identifier == part_id:
                found = True
                response_parts.append(updater(part))
                continue
        response_parts.append(part)

    if not found:
        return state
    return {**state, "activeTurn": {**active, "responseParts": response_parts}}


def _append_text(kind: str, content: Any) -> Callable[[Mapping[str, Any]], Any]:
    """Updater for ``chat/delta`` and ``chat/reasoning``: append to a matching part."""

    def updater(part: Mapping[str, Any]) -> Any:
        if part.get("kind") == kind:
            return {**part, "content": _as_text(part.get("content")) + _as_text(content)}
        return part

    return updater


# ─── Chat Reducer ────────────────────────────────────────────────────────────


def chat_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    """Pure-except-for-the-clock reducer for chat state. 29 action variants."""
    action_type = action.get("type")

    # ── Turn lifecycle ───────────────────────────────────────────────────────

    if action_type == "chat/turnStarted":
        next_state = {
            **state,
            "activeTurn": {
                "id": action.get("turnId"),
                "startedAt": action.get("startedAt"),
                "message": action.get("message"),
                "responseParts": [],
                "usage": None,
            },
        }
        next_state = {
            **next_state,
            "status": _with_status_flag(_summary_status(next_state), _STATUS_IS_READ, False),
            "modifiedAt": now_iso(),
        }

        # If this turn was auto-started from a pending message, remove it.
        queued_message_id = action.get("queuedMessageId")
        if queued_message_id:  # A string in both languages: "" and absent both skip.
            steering = next_state.get("steeringMessage")
            if _mget(steering, "id") == queued_message_id:
                next_state = {**next_state, "steeringMessage": None}
            queued = next_state.get("queuedMessages")
            # `if (next.queuedMessages)`: AN EMPTY ARRAY IS TRUTHY in JavaScript,
            # so this must test presence, not Python truthiness. With `[]` the
            # upstream branch runs and rewrites the field to undefined.
            if queued is not None:
                filtered = [m for m in _seq(queued) if _mget(m, "id") != queued_message_id]
                next_state = {
                    **next_state,
                    "queuedMessages": filtered if len(filtered) > 0 else None,
                }
        return next_state

    if action_type == "chat/delta":
        return _update_response_part(
            state,
            action.get("turnId"),
            action.get("partId"),
            _append_text(_MARKDOWN, action.get("content")),
        )

    if action_type == "chat/reasoning":
        return _update_response_part(
            state,
            action.get("turnId"),
            action.get("partId"),
            _append_text(_REASONING, action.get("content")),
        )

    if action_type == "chat/responsePart":
        active = state.get("activeTurn")
        if active is None or _mget(active, "id") != action.get("turnId"):
            return state
        return {
            **state,
            "activeTurn": {
                **active,
                "responseParts": [*_parts(active), action.get("part")],
            },
        }

    if action_type == "chat/turnComplete":
        return _end_turn(state, action.get("turnId"), _TURN_COMPLETE, action.get("duration"))

    if action_type == "chat/turnCancelled":
        return _end_turn(state, action.get("turnId"), _TURN_CANCELLED, action.get("duration"))

    if action_type == "chat/error":
        return _end_turn(
            state,
            action.get("turnId"),
            _TURN_ERROR,
            action.get("duration"),
            _STATUS_ERROR,
            action.get("error"),
        )

    if action_type == "chat/activityChanged":
        return {**state, "activity": action.get("activity")}

    if action_type == "chat/usage":
        active = state.get("activeTurn")
        if active is None or _mget(active, "id") != action.get("turnId"):
            return state
        return {**state, "activeTurn": {**active, "usage": action.get("usage")}}

    # ── Working directories ──────────────────────────────────────────────────

    if action_type == "chat/workingDirectorySet":
        directory = action.get("directory")
        listing = _seq(coalesce(state.get("workingDirectories"), []))
        if directory in listing:
            return state
        return {**state, "workingDirectories": [*listing, directory]}

    if action_type == "chat/workingDirectoryRemoved":
        listing = state.get("workingDirectories")
        # `if (!list)`: an empty array is truthy upstream, so presence is the
        # test. It falls through to `indexOf === -1` and no-ops either way.
        if listing is None:
            return state
        directory = action.get("directory")
        items = _seq(listing)
        if directory not in items:
            return state
        index = items.index(directory)
        return {**state, "workingDirectories": [*items[:index], *items[index + 1 :]]}

    # ── Tool call state machine ──────────────────────────────────────────────

    if action_type == "chat/toolCallStart":
        active = state.get("activeTurn")
        if active is None or _mget(active, "id") != action.get("turnId"):
            return state
        return {
            **state,
            "activeTurn": {
                **active,
                "responseParts": [
                    *_parts(active),
                    {
                        "kind": _TOOL_CALL,
                        "toolCall": {
                            "toolCallId": action.get("toolCallId"),
                            "toolName": action.get("toolName"),
                            "displayName": action.get("displayName"),
                            "intention": action.get("intention"),
                            "contributor": action.get("contributor"),
                            "_meta": action.get("_meta"),
                            "status": _STREAMING,
                        },
                    },
                ],
            },
        }

    if action_type == "chat/toolCallDelta":

        def delta_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _STREAMING:
                return tc
            updated = {**tc}
            meta = action.get("_meta")
            if meta is not None:
                updated["_meta"] = meta
            content = action.get("content")
            if content is not None:
                updated["partialInput"] = _as_text(coalesce(tc.get("partialInput"), "")) + _as_text(
                    content
                )
            updated["invocationMessage"] = coalesce(
                action.get("invocationMessage"), tc.get("invocationMessage")
            )
            return updated

        return _update_tool_call_in_parts(
            state, action.get("turnId"), action.get("toolCallId"), delta_updater
        )

    if action_type == "chat/toolCallReady":

        def ready_updater(tc: Mapping[str, Any]) -> Any:
            status = tc.get("status")
            if status not in _READY_SOURCE_STATES:
                return tc
            base = _tc_base_with_meta(tc, action.get("_meta"))
            base["contributor"] = _refine_contributor(
                tc.get("contributor"), action.get("contributor")
            )
            base["intention"] = coalesce(action.get("intention"), tc.get("intention"))
            tool_input = coalesce(
                action.get("toolInput"),
                None if status == _STREAMING else tc.get("toolInput"),
            )
            confirmed = action.get("confirmed")
            if confirmed:  # A string enum: falsy only when absent or "".
                return {
                    "status": _RUNNING,
                    **base,
                    "invocationMessage": action.get("invocationMessage"),
                    "toolInput": tool_input,
                    "confirmed": confirmed,
                }
            pending = tc if status == _PENDING_CONFIRMATION else None
            options = coalesce(action.get("options"), _mget(pending, "options"))
            ready = {
                "status": _PENDING_CONFIRMATION,
                **base,
                "invocationMessage": action.get("invocationMessage"),
                "toolInput": tool_input,
                "confirmationTitle": coalesce(
                    action.get("confirmationTitle"), _mget(pending, "confirmationTitle")
                ),
                "riskAssessment": coalesce(
                    action.get("riskAssessment"), _mget(pending, "riskAssessment")
                ),
                "edits": coalesce(action.get("edits"), _mget(pending, "edits")),
                "editable": coalesce(action.get("editable"), _mget(pending, "editable")),
            }
            # `...(options ? { options } : {})` -- options is an ARRAY, and an
            # empty array is truthy in JavaScript. Presence, not truthiness.
            if options is not None:
                ready["options"] = options
            return ready

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state, action.get("turnId"), action.get("toolCallId"), ready_updater
            )
        )

    if action_type == "chat/toolCallConfirmed":

        def confirmed_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _PENDING_CONFIRMATION:
                return tc
            base = _tc_base_with_meta(tc, action.get("_meta"))
            selected = _resolve_selected_option(tc.get("options"), action.get("selectedOptionId"))
            if action.get("approved"):
                edited = action.get("editedToolInput")
                # Only inline (string) input is replaceable; referenced input is
                # swapped by the host at the resource, not here.
                tool_input = (
                    edited
                    if edited is not None and isinstance(tc.get("toolInput"), str)
                    else tc.get("toolInput")
                )
                resolved = {
                    "status": _RUNNING,
                    **base,
                    "invocationMessage": tc.get("invocationMessage"),
                    "toolInput": tool_input,
                    "confirmed": action.get("confirmed"),
                }
            else:
                resolved = {
                    "status": _CANCELLED,
                    **base,
                    "invocationMessage": tc.get("invocationMessage"),
                    "toolInput": tc.get("toolInput"),
                    "reason": action.get("reason"),
                    "reasonMessage": action.get("reasonMessage"),
                    "userSuggestion": action.get("userSuggestion"),
                }
            # An object; `{}` is truthy in JavaScript, so presence is the test.
            if selected is not None:
                resolved["selectedOption"] = selected
            return resolved

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state, action.get("turnId"), action.get("toolCallId"), confirmed_updater
            )
        )

    if action_type == "chat/toolCallComplete":

        def complete_updater(tc: Mapping[str, Any]) -> Any:
            status = tc.get("status")
            if status not in _COMPLETE_SOURCE_STATES:
                return tc
            result = action.get("result")
            # A *successful* completion from `auth-required` is invalid:
            # execution never resumed after the challenge. Ignored as a no-op.
            if status == _AUTH_REQUIRED and _mget(result, "success"):
                return tc
            base = _tc_base_with_meta(tc, action.get("_meta"))
            post_confirmation = status in (_RUNNING, _AUTH_REQUIRED)
            confirmed = tc.get("confirmed") if post_confirmation else _CONFIRM_NOT_NEEDED
            selected = tc.get("selectedOption") if post_confirmation else None
            # Content produced before the call paused for auth is the only
            # content the tool ever produced, unless `result` overrides it.
            pre_auth_content = tc.get("content") if status == _AUTH_REQUIRED else None
            # Cancelling from `auth-required` always completes terminally: the
            # pending challenge is not a "pending result" a client can review,
            # so `requiresResultConfirmation` is ignored on that path.
            pending_result = (
                bool(action.get("requiresResultConfirmation")) and status != _AUTH_REQUIRED
            )
            finished = {
                "status": _PENDING_RESULT_CONFIRMATION if pending_result else _COMPLETED,
                **base,
                "invocationMessage": tc.get("invocationMessage"),
                "toolInput": tc.get("toolInput"),
                "confirmed": confirmed,
            }
            if selected is not None:
                finished["selectedOption"] = selected
            # An ARRAY: `[]` is truthy in JavaScript, so presence is the test.
            if pre_auth_content is not None:
                finished["content"] = pre_auth_content
            # `...action.result` -- the result fields are FLATTENED onto the tool
            # call, not nested. Spreading `undefined` is a no-op upstream.
            if isinstance(result, Mapping):
                finished.update(result)
            return finished

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state, action.get("turnId"), action.get("toolCallId"), complete_updater
            )
        )

    if action_type == "chat/toolCallResultConfirmed":

        def result_confirmed_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _PENDING_RESULT_CONFIRMATION:
                return tc
            base = _tc_base_with_meta(tc, action.get("_meta"))
            selected = tc.get("selectedOption")
            if action.get("approved"):
                reviewed = {
                    "status": _COMPLETED,
                    **base,
                    "invocationMessage": tc.get("invocationMessage"),
                    "toolInput": tc.get("toolInput"),
                    "confirmed": tc.get("confirmed"),
                    "success": tc.get("success"),
                    "pastTenseMessage": tc.get("pastTenseMessage"),
                    "content": tc.get("content"),
                    "structuredContent": tc.get("structuredContent"),
                    "error": tc.get("error"),
                }
            else:
                # The result is discarded: a denied result never becomes state.
                reviewed = {
                    "status": _CANCELLED,
                    **base,
                    "invocationMessage": tc.get("invocationMessage"),
                    "toolInput": tc.get("toolInput"),
                    "reason": _CANCEL_RESULT_DENIED,
                }
            if selected is not None:
                reviewed["selectedOption"] = selected
            return reviewed

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state,
                action.get("turnId"),
                action.get("toolCallId"),
                result_confirmed_updater,
            )
        )

    if action_type == "chat/toolCallContentChanged":

        def content_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _RUNNING:
                return tc
            updated = {**tc}
            meta = action.get("_meta")
            if meta is not None:
                updated["_meta"] = meta
            updated["content"] = action.get("content")
            return updated

        return _update_tool_call_in_parts(
            state, action.get("turnId"), action.get("toolCallId"), content_updater
        )

    if action_type == "chat/toolCallAuthRequired":

        def auth_required_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _RUNNING:
                return tc
            contributor = tc.get("contributor")
            # Invariant: auth-required only applies to MCP-contributed calls.
            if contributor is None or _mget(contributor, "kind") != _CONTRIBUTOR_MCP:
                return tc
            paused = {
                "status": _AUTH_REQUIRED,
                **_tc_base_with_meta(tc, action.get("_meta")),
                "contributor": contributor,
                "invocationMessage": tc.get("invocationMessage"),
                "toolInput": tc.get("toolInput"),
                "confirmed": tc.get("confirmed"),
            }
            selected = tc.get("selectedOption")
            if selected is not None:
                paused["selectedOption"] = selected
            content = tc.get("content")
            # An ARRAY: presence, not truthiness.
            if content is not None:
                paused["content"] = content
            paused["auth"] = action.get("auth")
            return paused

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state,
                action.get("turnId"),
                action.get("toolCallId"),
                auth_required_updater,
            )
        )

    if action_type == "chat/toolCallAuthResolved":

        def auth_resolved_updater(tc: Mapping[str, Any]) -> Any:
            if tc.get("status") != _AUTH_REQUIRED:
                return tc
            # `auth` is intentionally not carried over: the challenge is gone.
            resumed = {
                "status": _RUNNING,
                **_tc_base_with_meta(tc, action.get("_meta")),
                "invocationMessage": tc.get("invocationMessage"),
                "toolInput": tc.get("toolInput"),
                "confirmed": tc.get("confirmed"),
            }
            selected = tc.get("selectedOption")
            if selected is not None:
                resumed["selectedOption"] = selected
            content = tc.get("content")
            if content is not None:
                resumed["content"] = content
            return resumed

        return _refresh_summary_status(
            _update_tool_call_in_parts(
                state,
                action.get("turnId"),
                action.get("toolCallId"),
                auth_resolved_updater,
            )
        )

    # ── Truncation ───────────────────────────────────────────────────────────

    if action_type == "chat/truncated":
        turn_id = action.get("turnId")
        existing_turns = _seq(state.get("turns"))
        if turn_id is None:
            turns: list[Any] = []
        else:
            index = next(
                (i for i, t in enumerate(existing_turns) if _mget(t, "id") == turn_id),
                -1,
            )
            if index < 0:
                return state
            turns = existing_turns[: index + 1]
        next_state = {
            **state,
            "turns": turns,
            "activeTurn": None,
            "modifiedAt": now_iso(),
        }
        if turn_id is None:
            # Upstream `delete`s the key: the window is no longer a tail, so
            # there is nothing older left to page.
            next_state.pop("turnsNextCursor", None)
        return {**next_state, "status": _summary_status(next_state)}

    if action_type == "chat/turnsLoaded":
        existing_turns = _seq(state.get("turns"))
        existing_ids = {_mget(t, "id") for t in existing_turns}
        older = [t for t in _seq(action.get("turns")) if _mget(t, "id") not in existing_ids]
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
        existing = _find_open_input_request_part(response_parts, action.get("requestId"))
        if existing is None:
            return state
        index, part = existing
        request = part.get("request")
        answers = dict(coalesce(_mget(request, "answers"), {}) or {})
        question_id = action.get("questionId")
        answer = action.get("answer")
        if answer is None:
            answers.pop(question_id, None)
        else:
            answers[question_id] = answer
        response_parts[index] = {
            **part,
            "request": {
                **(request if isinstance(request, Mapping) else {}),
                "answers": answers if len(answers) > 0 else None,
            },
        }
        return {
            **state,
            "activeTurn": {**active, "responseParts": response_parts},
            "modifiedAt": now_iso(),
        }

    if action_type == "chat/inputCompleted":
        active = state.get("activeTurn")
        if active is None:
            return state
        response_parts = _parts(active)
        existing = _find_open_input_request_part(response_parts, action.get("requestId"))
        if existing is None:
            return state
        index, part = existing
        request = part.get("request")
        final_answers = {
            **(coalesce(_mget(request, "answers"), {}) or {}),
            **(coalesce(action.get("answers"), {}) or {}),
        }
        response_parts[index] = {
            **part,
            "request": {
                **(request if isinstance(request, Mapping) else {}),
                "answers": final_answers if len(final_answers) > 0 else None,
            },
            "response": action.get("response"),
        }
        next_state = {**state, "activeTurn": {**active, "responseParts": response_parts}}
        return {
            **next_state,
            "status": _summary_status(next_state),
            "modifiedAt": now_iso(),
        }

    # ── Pending messages ─────────────────────────────────────────────────────

    if action_type == "chat/pendingMessageSet":
        entry = {"id": action.get("id"), "message": action.get("message")}
        if action.get("kind") == _PENDING_MESSAGE_STEERING:
            return {**state, "steeringMessage": entry}
        existing_queue = _seq(coalesce(state.get("queuedMessages"), []))
        index = next(
            (i for i, m in enumerate(existing_queue) if _mget(m, "id") == action.get("id")),
            -1,
        )
        if index >= 0:
            updated_queue = list(existing_queue)
            updated_queue[index] = entry
            return {**state, "queuedMessages": updated_queue}
        return {**state, "queuedMessages": [*existing_queue, entry]}

    if action_type == "chat/pendingMessageRemoved":
        if action.get("kind") == _PENDING_MESSAGE_STEERING:
            steering = state.get("steeringMessage")
            if steering is None or _mget(steering, "id") != action.get("id"):
                return state
            return {**state, "steeringMessage": None}
        queued = state.get("queuedMessages")
        if queued is None:
            return state
        items = _seq(queued)
        filtered = [m for m in items if _mget(m, "id") != action.get("id")]
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
        by_id: dict[Any, Any] = {}
        for message in items:
            by_id[_mget(message, "id")] = message  # A JS Map: last write wins.
        ordered: set[Any] = set()
        reordered: list[Any] = []
        for identifier in _seq(action.get("order")):
            if identifier in by_id and identifier not in ordered:
                ordered.add(identifier)
                reordered.append(by_id[identifier])
        # Append anything not mentioned in `order`, preserving the original
        # order, so a client with a stale view never silently drops a message.
        for message in items:
            if _mget(message, "id") not in ordered:
                reordered.append(message)
        return {**state, "queuedMessages": reordered}

    # ── Draft ────────────────────────────────────────────────────────────────

    if action_type == "chat/draftChanged":
        return {**state, "draft": action.get("draft")}

    # Unknown action: return the state unchanged. Never raise -- upstream's
    # `softAssertNever` logs and degrades so a peer speaking a newer version of
    # the protocol still converges. There is no fixture for this on chat.
    return state
