"""Constructors for the outbound actions this client dispatches.

The turn-scoped chat actions, and the single client-dispatchable changeset one.

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

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from agent_host_protocol.reducers.clock import now_iso
from agent_host_protocol.types import JsonObject

from agent_host_client.client.errors import InvalidArgument

__all__ = [
    "ConfirmationReason",
    "DenialReason",
    "files_review_changed",
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
        raise InvalidArgument(f"{field} is required on this action and was empty")
    return value


def turn_started(
    turn_id: str,
    *,
    text: str,
    model: str | Mapping[str, Any] | None = None,
    attachments: Sequence[Mapping[str, Any]] | None = None,
    queued_message_id: str | None = None,
    started_at: str | None = None,
) -> JsonObject:
    """`chat/turnStarted`; required `[type, turnId, startedAt, message]`, and
    `Message` requires `[text, origin]`.

    `origin` is fixed at `user` because that is the only kind a client may
    produce: "A client is only allowed to send `MessageKind.User` messages."

    *model* is `Message.model`, a `ModelSelection` **object** -- `{id, config?}`
    with `id` required -- not a bare string. A string here is wrapped into
    `{"id": model}` rather than written through, because the bare string is
    wire-invalid: a validating peer rejects the action and a lenient one
    freezes the malformed `Message` into every subscriber's transcript.

    *attachments* is `Message.attachments`, and *queued_message_id* is the
    action's own `queuedMessageId` -- both optional upstream, and both
    unreachable without a named parameter because these constructors
    deliberately take no `**extra`.

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
        message["model"] = (
            {"id": _identifier("model.id", model)} if isinstance(model, str) else dict(model)
        )
    if attachments is not None:
        message["attachments"] = [dict(attachment) for attachment in attachments]
    action: JsonObject = {
        "type": "chat/turnStarted",
        "turnId": _identifier("turnId", turn_id),
        "startedAt": started_at or now_iso(),
        "message": message,
    }
    if queued_message_id is not None:
        action["queuedMessageId"] = queued_message_id
    return action


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
    edited_tool_input: str | None = None,
) -> JsonObject:
    """`chat/toolCallConfirmed` with `approved: true`; required
    `[turnId, toolCallId, type, approved, confirmed]`.

    The option field is `selectedOptionId`, not `optionId`. The reducer resolves
    it against the call's own `options` and stores the whole option
    (`_resolve_selected_option`), so a mis-keyed name silently drops the
    difference between "approve once" and "approve for this session" -- the
    action still applies, and nothing anywhere records which button was pressed.

    *edited_tool_input* is a **string** because `editedToolInput?: string` is:
    only the inline form of `ToolInput` is client-editable ("for inline
    `toolInput`, the reducer replaces the state value directly"), so a dict
    here would put a shape on the wire no peer's reducer can render.
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
    user_suggestion: Mapping[str, Any] | None = None,
    reason_message: str | Mapping[str, Any] | None = None,
) -> JsonObject:
    """`chat/toolCallConfirmed` with `approved: false`; required
    `[turnId, toolCallId, type, approved, reason]`.

    *user_suggestion* is a `Message` ("what the user suggested doing instead")
    and *reason_message* a `StringOrMarkdown` -- both optional on
    `ChatToolCallDeniedAction`, and both unreachable without named parameters
    because these constructors deliberately take no `**extra`.
    """
    action: JsonObject = {
        "type": "chat/toolCallConfirmed",
        "turnId": _identifier("turnId", turn_id),
        "toolCallId": _identifier("toolCallId", tool_call_id),
        "approved": False,
        "reason": reason,
    }
    if selected_option_id is not None:
        action["selectedOptionId"] = selected_option_id
    if user_suggestion is not None:
        action["userSuggestion"] = dict(user_suggestion)
    if reason_message is not None:
        action["reasonMessage"] = (
            dict(reason_message) if isinstance(reason_message, Mapping) else reason_message
        )
    return action


def tool_call_complete(
    turn_id: str,
    tool_call_id: str,
    *,
    result: Mapping[str, Any],
    requires_result_confirmation: bool | None = None,
) -> JsonObject:
    """`chat/toolCallComplete`; required `[turnId, toolCallId, type, result]`.

    This is the one a client dispatches for a tool only it can run. Without
    `turnId` the reducer no-ops while the host resolves the provider's future
    off `toolCallId` alone: the work happened, the agent used the answer, and
    every transcript records the call as `cancelled`/`skipped`.

    *requires_result_confirmation* asks for a result review before the call
    finalises -- optional on the action, and without a named parameter a client
    tool could never request one at all.
    """
    action: JsonObject = {
        "type": "chat/toolCallComplete",
        "turnId": _identifier("turnId", turn_id),
        "toolCallId": _identifier("toolCallId", tool_call_id),
        "result": dict(result),
    }
    if requires_result_confirmation is not None:
        action["requiresResultConfirmation"] = requires_result_confirmation
    return action


def tool_call_result_confirmed(turn_id: str, tool_call_id: str, *, approved: bool) -> JsonObject:
    """`chat/toolCallResultConfirmed`; required
    `[turnId, toolCallId, type, approved]`."""
    return {
        "type": "chat/toolCallResultConfirmed",
        "turnId": _identifier("turnId", turn_id),
        "toolCallId": _identifier("toolCallId", tool_call_id),
        "approved": approved,
    }


def files_review_changed(file_ids: Sequence[str], reviewed: bool) -> JsonObject:
    """`changeset/filesReviewChanged`; required `[type, files, reviewed]`.

    The only client-dispatchable changeset action. Both guards below fail the
    same way the missing `turnId` did -- the host accepts, numbers and
    broadcasts the action, and every reducer matches nothing:

    * an **empty list** matches no file, and "if none match, the action is a
      no-op". The optimistic apply is a no-op too, so nothing anywhere reports
      that a tick box was asked to move and did not.
    * an **empty id** is a real key, not a missing one. The reducer's
      `ids.has(f.id)` is SameValueZero over `new Set(action.files)`, so `""`
      matches a file whose id is `""` and nothing else -- silently.

    `files` is an array of ids and **not** a string: upstream builds a `Set`
    from it, and a `Set` consumes a string character by character, so passing
    `"a.py"` marks the files with ids `"a"`, `"."`, `"p"` and `"y"`. That is
    upstream's real behaviour and it is why this signature takes a sequence.
    """
    ids = list(file_ids)
    if not ids:
        raise InvalidArgument("changeset/filesReviewChanged with no file ids is a silent no-op")
    return {
        "type": "changeset/filesReviewChanged",
        "files": [_identifier("file id", identifier) for identifier in ids],
        "reviewed": reviewed,
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
