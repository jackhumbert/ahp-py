"""Redaction after publishing: the actions, the marker and the pure reducer.

This is an ahp-py extension ahead of the spec. The upstream request is
``chat/partRedacted`` plus a message variant (ahp-host ``docs/requests.md``,
entry 13). A host that must withdraw content it already published (a leaked
secret, a mistaken paste) rewrites a response part, or a turn's user message, to
a marker that uses only existing part kinds, so every stock client can render
it.

A host sends :data:`PART_REDACTED` / :data:`MESSAGE_REDACTED` ONLY to a
connection that put :data:`REDACTION_CAPABILITY` in ``InitializeParams._meta``
(or ``ReconnectParams._meta``): a stock client must never receive an action it
does not know. Everyone else gets the redacted state in their next snapshot. The
stock chat reducer ignores these actions (invariant 3), so
:func:`apply_redaction` is the reducer semantics host and client share.

Only the shared view is redacted, never an agent's own context.

Everything here is pure and stdlib-only. Nothing raises on peer data: a target
that does not exist, or is of a kind that cannot be redacted, leaves the state
unchanged (the very same object).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "MESSAGE_REDACTED",
    "PART_REDACTED",
    "REDACTABLE_PART_KINDS",
    "REDACTED_TEXT",
    "REDACTION_ACTIONS",
    "REDACTION_CAPABILITY",
    "REDACTION_META_KEY",
    "TOOL_CALL_KEPT_KEYS",
    "TOOL_CALL_TEXT_KEYS",
    "apply_redaction",
    "find_part",
    "find_turn",
    "is_redaction_action",
    "marker_of",
    "message_redacted",
    "part_id_of",
    "part_redacted",
]

#: Key in ``InitializeParams._meta`` / ``ReconnectParams._meta`` (value ``{}``, a
#: presence flag like ``ClientCapabilities``); also the key the host puts in
#: ``InitializeResult._meta``.
REDACTION_CAPABILITY: Final = "ahp-py/redaction"
#: Key in a redacted part's, tool call's or message's ``_meta``; the value is
#: ``{"reason": str, "redactedAt": str}``.
REDACTION_META_KEY: Final = "ahp-py/redaction"
#: The extension action redacting one response part.
PART_REDACTED: Final = "ahp-py/chat/partRedacted"
#: The extension action redacting a turn's user message.
MESSAGE_REDACTED: Final = "ahp-py/chat/messageRedacted"
REDACTION_ACTIONS: Final = frozenset({PART_REDACTED, MESSAGE_REDACTED})
#: The replacement text; the same string ahp-host-flow's ``engine/redact.py`` MASK uses.
REDACTED_TEXT: Final = "[redacted]"
#: The part kinds that carry an ``id`` (or tool call id) and content.
REDACTABLE_PART_KINDS: Final = ("markdown", "reasoning", "toolCall")
#: Every required field of every ``ToolCall*State`` that is not content: kept so
#: the redacted tool call still validates and its state machine is unchanged.
TOOL_CALL_KEPT_KEYS: Final = (
    "toolCallId",
    "toolName",
    "displayName",
    "status",
    "contributor",
    "confirmed",
    "success",
    "reason",
    "auth",
)
#: Tool call fields replaced by :data:`REDACTED_TEXT` when present.
TOOL_CALL_TEXT_KEYS: Final = (
    "invocationMessage",
    "pastTenseMessage",
    "intention",
    "reasonMessage",
    "confirmationTitle",
    "toolInput",
    "partialInput",
)
#: Fields of a user message that are not content.
_MESSAGE_KEPT_KEYS: Final = ("origin", "model", "agent")


def part_redacted(
    turn_id: str,
    part_id: str,
    reason: str,
    redacted_at: str,
    *,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The :data:`PART_REDACTED` action.

    ``meta`` is the ``_meta`` entries the redacted part keeps, already scrubbed by
    the host (for example a flow descriptor from ``flow.redacted_step``).
    """
    action: dict[str, Any] = {
        "type": PART_REDACTED,
        "turnId": turn_id,
        "partId": part_id,
        "reason": reason,
        "redactedAt": redacted_at,
    }
    if meta is not None:
        action["meta"] = dict(meta)
    return action


def message_redacted(turn_id: str, reason: str, redacted_at: str) -> dict[str, Any]:
    """The :data:`MESSAGE_REDACTED` action."""
    return {
        "type": MESSAGE_REDACTED,
        "turnId": turn_id,
        "reason": reason,
        "redactedAt": redacted_at,
    }


def is_redaction_action(action: Any) -> bool:
    return isinstance(action, Mapping) and action.get("type") in REDACTION_ACTIONS


def _same(value: Any, expected: str) -> bool:
    return isinstance(value, str) and value == expected


def find_turn(chat_state: Any, turn_id: Any) -> Mapping[str, Any] | None:
    """The turn with ``turn_id``: ``activeTurn`` first, then ``turns`` in order.

    Ids compare with ``isinstance(x, str) and x == turn_id``, never Python
    truthiness or ``==`` coercion: the ``===`` the upstream reducer would use.
    """
    if not isinstance(chat_state, Mapping) or not isinstance(turn_id, str):
        return None
    active = chat_state.get("activeTurn")
    if isinstance(active, Mapping) and _same(active.get("id"), turn_id):
        return active
    turns = chat_state.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if isinstance(turn, Mapping) and _same(turn.get("id"), turn_id):
                return turn
    return None


def part_id_of(part: Any) -> str | None:
    """A part's identity: the tool call id for a ``toolCall``, else its ``id``.

    The same rule as the chat reducer's ``_update_response_part``.
    """
    if not isinstance(part, Mapping):
        return None
    if part.get("kind") == "toolCall":
        call = part.get("toolCall")
        identifier = call.get("toolCallId") if isinstance(call, Mapping) else None
    else:
        identifier = part.get("id")
    return identifier if isinstance(identifier, str) else None


def find_part(chat_state: Any, turn_id: Any, part_id: Any) -> Mapping[str, Any] | None:
    """The first response part of the turn whose :func:`part_id_of` is ``part_id``."""
    turn = find_turn(chat_state, turn_id)
    if turn is None or not isinstance(part_id, str):
        return None
    parts = turn.get("responseParts")
    if not isinstance(parts, list):
        return None
    for part in parts:
        if _same(part_id_of(part), part_id):
            return part  # type: ignore[no-any-return]
    return None


def marker_of(value: Any) -> Mapping[str, Any] | None:
    """The ``{reason, redactedAt}`` marker of a redacted part or message, or ``None``."""
    if not isinstance(value, Mapping):
        return None
    holder: Any = value
    if value.get("kind") == "toolCall":
        holder = value.get("toolCall")
    if not isinstance(holder, Mapping):
        return None
    meta = holder.get("_meta")
    if not isinstance(meta, Mapping):
        return None
    marker = meta.get(REDACTION_META_KEY)
    return marker if isinstance(marker, Mapping) else None


def apply_redaction(chat_state: Any, action: Mapping[str, Any]) -> Any:
    """Apply one redaction action to a reduced chat state; pure, never raises.

    Returns ``chat_state`` itself when the action is not a redaction, is
    malformed, or its target is missing or cannot be redacted. Parts without an id
    (``systemNotification``, ``contentRef``, ``error``) and ``inputRequest`` parts
    cannot be targeted. Ids and part order stay stable and no clock is read:
    ``redactedAt`` comes from the action.

    A markdown or reasoning part keeps only ``kind``, ``id`` and a marker; a tool
    call keeps an allowlist (an unknown field may carry content) with its status,
    so the tool-call state machine is unchanged. A later ``chat/delta`` on a
    redacted active markdown part appends after ``"[redacted]"``: new content,
    published normally.
    """
    if not isinstance(chat_state, Mapping) or not isinstance(action, Mapping):
        return chat_state
    kind = action.get("type")
    turn_id = action.get("turnId")
    reason = action.get("reason")
    redacted_at = action.get("redactedAt")
    if kind not in REDACTION_ACTIONS:
        return chat_state
    if not (isinstance(turn_id, str) and isinstance(reason, str) and isinstance(redacted_at, str)):
        return chat_state
    turn = find_turn(chat_state, turn_id)
    if turn is None:
        return chat_state
    marker = {"reason": reason, "redactedAt": redacted_at}

    new_turn: dict[str, Any]
    if kind == PART_REDACTED:
        part_id = action.get("partId")
        parts = turn.get("responseParts")
        if not isinstance(part_id, str) or not isinstance(parts, list):
            return chat_state
        index = next((i for i, p in enumerate(parts) if _same(part_id_of(p), part_id)), None)
        if index is None:
            return chat_state
        part = parts[index]
        if part.get("kind") not in REDACTABLE_PART_KINDS:
            return chat_state
        extra = action.get("meta")
        new_meta = {**(extra if isinstance(extra, Mapping) else {}), REDACTION_META_KEY: marker}
        new_parts = [*parts[:index], _redact_part(part, new_meta), *parts[index + 1 :]]
        new_turn = {**turn, "responseParts": new_parts}
    else:
        message = turn.get("message")
        if not isinstance(message, Mapping):
            return chat_state
        new_message: dict[str, Any] = {"text": REDACTED_TEXT}
        for key in _MESSAGE_KEPT_KEYS:
            if key in message:
                new_message[key] = message[key]
        new_message["_meta"] = {REDACTION_META_KEY: marker}
        new_turn = {**turn, "message": new_message}

    if turn is chat_state.get("activeTurn"):
        return {**chat_state, "activeTurn": new_turn}
    turns = chat_state.get("turns")
    assert isinstance(turns, list)  # find_turn found it there
    return {**chat_state, "turns": [new_turn if t is turn else t for t in turns]}


def _redact_part(part: Mapping[str, Any], new_meta: dict[str, Any]) -> dict[str, Any]:
    if part.get("kind") != "toolCall":
        return {
            "kind": part["kind"],
            "id": part.get("id"),
            "content": REDACTED_TEXT,
            "_meta": new_meta,
        }
    call = part["toolCall"]
    kept: dict[str, Any] = {k: call[k] for k in TOOL_CALL_KEPT_KEYS if k in call}
    for key in TOOL_CALL_TEXT_KEYS:
        if key in call:
            kept[key] = REDACTED_TEXT
    if "content" in call:
        kept["content"] = [{"type": "text", "text": REDACTED_TEXT}]
    if "error" in call:
        kept["error"] = {"message": REDACTED_TEXT}
    kept["_meta"] = new_meta
    return {"kind": "toolCall", "toolCall": kept}
