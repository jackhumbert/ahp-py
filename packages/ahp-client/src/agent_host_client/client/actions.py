"""Constructors for the turn-scoped chat actions this client dispatches.

Five call sites across `api/` and `serve/` each assembled one of these as a dict
literal, and every one of them omitted `turnId` -- required on all of them, and
the field the chat reducer matches **before** it will touch anything:
`_update_tool_call_in_parts` and `_end_turn` both return the state unchanged
when `activeTurn.id != action.turnId`. The omission is silent on both peers,
because the host resolves its pending future off `toolCallId` alone: the tool
really runs, the agent really gets the answer, and every mirror shows the call
cancelled as `skipped`.

So the requirement lives here, in signatures `mypy --strict` checks, rather than
in five dict literals a sixth site can copy without it. Not re-exported from
`agent_host_client.client`: these are the library's own constructors, and a
consumer reaches the same shapes through `ToolCallReady.approve()`,
`Chat.cancel()` and `InputResponder`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from agent_host_protocol.reducers.clock import now_iso
from agent_host_protocol.types import JsonObject

__all__ = [
    "ConfirmationReason",
    "DenialReason",
    "tool_call_approved",
    "tool_call_complete",
    "tool_call_denied",
    "tool_call_result_confirmed",
    "tool_failure_result",
    "turn_cancelled",
    "turn_started",
]

#: `ToolCallConfirmationReason`: how a tool call was confirmed for execution.
ConfirmationReason = Literal["not-needed", "user-action", "setting"]
#: `ChatToolCallDeniedAction.reason`.
DenialReason = Literal["denied", "skipped"]


def _identifier(field: str, value: str) -> str:
    """Reject an empty id rather than putting one on the wire.

    A required parameter stops a call site *forgetting* the field; this stops it
    passing an empty one, which reduces to the same silent no-op. Loud is the
    whole point -- the defect this module exists for was invisible on both
    sides.
    """
    if not value:
        raise ValueError(f"{field} is required on this action and was empty")
    return value


def turn_started(
    turn_id: str, *, text: str, model: str | None = None, started_at: str | None = None
) -> JsonObject:
    """`chat/turnStarted`; required `[type, turnId, startedAt, message]`, and
    `Message` requires `[text, origin]`.

    `origin` is fixed at `user` because that is the only kind a client may
    produce: "A client is only allowed to send `MessageKind.User` messages."

    `startedAt` is ours to stamp. The reducer copies it into
    `ActiveTurn.startedAt` verbatim, so omitting it stores `null` on every peer
    for the life of the transcript -- and the host republishes our action rather
    than filling it in. Stamped through the shared clock so the format is
    JavaScript's `toISOString()` (three-digit millis and a `Z`, which
    `datetime.isoformat()` does not produce) and so a frozen-clock test sees a
    stable value.
    """
    message: JsonObject = {"text": text, "origin": {"kind": "user"}}
    if model is not None:
        message["model"] = model
    return {
        "type": "chat/turnStarted",
        "turnId": _identifier("turnId", turn_id),
        "startedAt": started_at or now_iso(),
        "message": message,
    }


def turn_cancelled(turn_id: str, *, duration_ms: float) -> JsonObject:
    """`chat/turnCancelled`; required `[type, turnId, duration]`.

    *duration_ms* must be measured on the producer's own clock: the spec says
    clients "MUST NOT derive this by subtracting timestamps -- cross-client
    clocks may differ" and consumers "MUST treat it as opaque,
    producer-supplied data". `ActiveTurn.startedAt` belongs to whoever started
    the turn, so it is exactly the timestamp not to subtract.
    """
    return {
        "type": "chat/turnCancelled",
        "turnId": _identifier("turnId", turn_id),
        "duration": duration_ms,
    }


def tool_call_approved(
    turn_id: str,
    tool_call_id: str,
    *,
    confirmed: ConfirmationReason = "user-action",
    selected_option_id: str | None = None,
    edited_tool_input: Any = None,
) -> JsonObject:
    """`chat/toolCallConfirmed` with `approved: true`; required
    `[turnId, toolCallId, type, approved, confirmed]`.

    The option field is `selectedOptionId`, not `optionId`. The reducer resolves
    it against the call's own `options` and stores the whole option
    (`_resolve_selected_option`), so a mis-keyed name silently drops the
    difference between "approve once" and "approve for this session" -- the
    action still applies, and nothing anywhere records which button was pressed.
    """
    action: JsonObject = {
        "type": "chat/toolCallConfirmed",
        "turnId": _identifier("turnId", turn_id),
        "toolCallId": _identifier("toolCallId", tool_call_id),
        "approved": True,
        "confirmed": confirmed,
    }
    if selected_option_id is not None:
        action["selectedOptionId"] = selected_option_id
    if edited_tool_input is not None:
        action["editedToolInput"] = edited_tool_input
    return action


def tool_call_denied(
    turn_id: str,
    tool_call_id: str,
    *,
    reason: DenialReason = "denied",
    selected_option_id: str | None = None,
) -> JsonObject:
    """`chat/toolCallConfirmed` with `approved: false`; required
    `[turnId, toolCallId, type, approved, reason]`."""
    action: JsonObject = {
        "type": "chat/toolCallConfirmed",
        "turnId": _identifier("turnId", turn_id),
        "toolCallId": _identifier("toolCallId", tool_call_id),
        "approved": False,
        "reason": reason,
    }
    if selected_option_id is not None:
        action["selectedOptionId"] = selected_option_id
    return action


def tool_call_complete(turn_id: str, tool_call_id: str, *, result: Mapping[str, Any]) -> JsonObject:
    """`chat/toolCallComplete`; required `[turnId, toolCallId, type, result]`.

    This is the one a client dispatches for a tool only it can run. Without
    `turnId` the reducer no-ops while the host resolves the provider's future
    off `toolCallId` alone: the work happened, the agent used the answer, and
    every transcript records the call as `cancelled`/`skipped`.
    """
    return {
        "type": "chat/toolCallComplete",
        "turnId": _identifier("turnId", turn_id),
        "toolCallId": _identifier("toolCallId", tool_call_id),
        "result": dict(result),
    }


def tool_call_result_confirmed(turn_id: str, tool_call_id: str, *, approved: bool) -> JsonObject:
    """`chat/toolCallResultConfirmed`; required
    `[turnId, toolCallId, type, approved]`."""
    return {
        "type": "chat/toolCallResultConfirmed",
        "turnId": _identifier("turnId", turn_id),
        "toolCallId": _identifier("toolCallId", tool_call_id),
        "approved": approved,
    }


def tool_failure_result(message: str) -> JsonObject:
    """A `ToolCallResult` for an executor that raised.

    `ToolCallResult` requires `[success, pastTenseMessage]`, content blocks are
    discriminated by `type` (not `kind`), and there is no `isError` -- MCP's
    spelling, which is not this one. A failure is `success: false`.
    """
    return {
        "success": False,
        "pastTenseMessage": "The tool failed",
        "content": [{"type": "text", "text": message}],
        "error": {"message": message},
    }
