"""Another chat's turns, as text Claude can read.

Two places hand Claude a conversation it did not have itself:

* a chat attachment (`MessageChatAttachment`): "the host MUST resolve the
  referenced chat's retained transcript ... and supply it as model context",
  which ahp-host does as `UserMessage.attached_chats`;
* a fork or side chat whose source cannot be forked in Claude Code (its turns
  predate this adapter's transcript marks, or its session is not running
  here): the copied turns are what the user sees, so Claude gets them as text
  rather than nothing.

The turns are the host's published `Turn` shape. What reaches the text: the
user's message, the answer's Markdown, each tool call as one line (its
past-tense line, or what it was doing), system notes and errors. Reasoning is
left out - it is the model's working, not the conversation. Long transcripts
keep their end, which is the part a follow-up is about, and say what was cut.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = ["LIMIT", "transcript"]

#: Characters of transcript handed over at most, like a pasted text attachment.
LIMIT: Final = 200_000


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("markdown"), str):
        return str(value["markdown"])
    return ""


def _part(part: Any) -> str | None:
    if not isinstance(part, Mapping):
        return None
    kind = part.get("kind")
    if kind == "markdown":
        return _text(part.get("content")) or None
    if kind == "toolCall":
        call = part.get("toolCall")
        if not isinstance(call, Mapping):
            return None
        name = _text(call.get("displayName")) or _text(call.get("toolName")) or "tool"
        what = _text(call.get("pastTenseMessage")) or _text(call.get("invocationMessage"))
        return f"[{name}: {what}]" if what else f"[{name}]"
    if kind == "systemNotification":
        content = _text(part.get("content"))
        return f"[{content}]" if content else None
    if kind == "error":
        message = _text(part.get("message")) or _text(part.get("error"))
        return f"[Error: {message}]" if message else "[Error]"
    return None


def _turn(turn: Mapping[str, Any]) -> str:
    message = turn.get("message")
    asked = _text(message.get("text")) if isinstance(message, Mapping) else ""
    parts = turn.get("responseParts")
    answered = [
        text
        for text in (_part(p) for p in (parts if isinstance(parts, Sequence) else ()))
        if text is not None
    ]
    lines = [f"User: {asked}"] if asked else []
    if answered:
        lines.append("Assistant: " + "\n".join(answered))
    return "\n".join(lines)


def transcript(turns: Sequence[Mapping[str, Any]], *, limit: int = LIMIT) -> str:
    """The turns as one block of text, cut at the start if longer than *limit*."""
    text = "\n\n".join(t for t in (_turn(turn) for turn in turns if isinstance(turn, Mapping)) if t)
    if len(text) <= limit:
        return text
    return "[Earlier turns omitted]\n\n" + text[-limit:]
