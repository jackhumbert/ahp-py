"""An AHP message as an ACP prompt: its text, its attachments, attached chats.

ACP's `session/prompt` takes `ContentBlock`s (stable). Every agent takes
`text` and `resource_link`; `image`, `audio` and embedded `resource` blocks
only when its `initialize` said so (`promptCapabilities.image`, `.audio`,
`.embeddedContext`). So each AHP attachment becomes the richest block the
agent can take, and one it can take none of is left out with a log line --
never a failed turn:

- **`resource`** (a file by URI): the file itself, embedded as text, when the
  agent takes embedded context and the file is a small text file inside the
  served folders (read here: the URI a client sends is a folder-tree URI the
  agent could not open); otherwise a `resource_link` to its real path, which
  the agent can open itself. A URI that is neither a served file nor web
  address means nothing to an agent and is left out. A text selection is
  said in a text block after it.
- **`embeddedResource`** (inline base64): an `image` or `audio` block for one
  of those, when the agent takes it; otherwise an embedded `resource` (text
  if it decodes, else a blob) when it takes embedded context.
- **`simple`**: its `modelRepresentation` as text. One without (a slash
  command's completion) is already in the message text.
- **`chat`**: the transcript the host resolved (`UserMessage.attached_chats`,
  "supply it as model context"), as an embedded markdown resource named by
  the chat's URI, or as text for an agent without embedded context.
- **`annotations`** live on a host channel this adapter does not read: left out.

A forked session whose agent could not fork is given the copied transcript
the same way, ahead of the message (see `AcpSession`).
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from ahp_host.provider.base import AttachedChat, UserMessage

from ahp_host_acp.roots import Roots

log = logging.getLogger(__name__)

#: Larger files are linked, not embedded.
MAX_EMBED: Final = 1024 * 1024


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def _turn_text(turn: Mapping[str, Any]) -> tuple[str, str]:
    message = turn.get("message")
    asked = message.get("text") if isinstance(message, Mapping) else None
    parts = turn.get("responseParts")
    answer = "".join(
        str(part.get("content", ""))
        for part in (parts if isinstance(parts, list) else ())
        if isinstance(part, Mapping) and part.get("kind") == "markdown"
    )
    return (asked if isinstance(asked, str) else ""), answer


def transcript(turns: Sequence[Mapping[str, Any]]) -> str:
    """Turns as markdown: what the user said, and what the agent answered."""
    lines: list[str] = []
    for turn in turns:
        asked, answer = _turn_text(turn)
        lines.append(f"**User:** {asked}".rstrip())
        lines.append(f"**Agent:** {answer}".rstrip())
        lines.append("")
    return "\n".join(lines).strip() or "(no completed turns)"


def chat_block(
    uri: str, label: str, turns: Sequence[Mapping[str, Any]], embedded: bool
) -> dict[str, Any]:
    """A transcript as context: an embedded resource, or text."""
    body = transcript(turns)
    if embedded:
        return {
            "type": "resource",
            "resource": {"uri": uri, "mimeType": "text/markdown", "text": f"# {label}\n\n{body}"},
        }
    return text(f"Context -- {label} ({uri}):\n\n{body}")


class PromptBuilder:
    """Turns one message into the blocks this agent can take."""

    def __init__(self, roots: Roots, prompt_capabilities: Mapping[str, Any]) -> None:
        self._roots = roots
        self._image = prompt_capabilities.get("image") is True
        self._audio = prompt_capabilities.get("audio") is True
        self.embedded = prompt_capabilities.get("embeddedContext") is True

    def blocks(self, message: UserMessage) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [text(message.text)]
        attachments = message.raw.get("attachments")
        for attachment in attachments if isinstance(attachments, list) else ():
            if isinstance(attachment, Mapping):
                blocks.extend(self._attachment(attachment))
        for chat in message.attached_chats:
            blocks.append(self._chat(chat))
        return blocks

    def _chat(self, chat: AttachedChat) -> dict[str, Any]:
        label = chat.label or "Attached chat"
        if chat.end_turn:
            label = f"{label}, through turn {chat.end_turn}"
        return chat_block(chat.resource, label, chat.turns, self.embedded)

    def _attachment(self, attachment: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = attachment.get("type")
        label = str(attachment.get("label") or "attachment")
        if kind == "resource":
            return self._resource(attachment, label)
        if kind == "embeddedResource":
            return self._inline(attachment, label)
        if kind == "simple":
            shown = attachment.get("modelRepresentation")
            return [text(shown)] if isinstance(shown, str) and shown.strip() else []
        if kind == "chat":
            return []  # resolved by the host: `attached_chats`
        log.info("left out attachment %r (%s): nothing an ACP agent can take", label, kind)
        return []

    def _resource(self, attachment: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
        uri = attachment.get("uri")
        if not isinstance(uri, str):
            return []
        mime = attachment.get("contentType")
        real = self._roots.real_path(uri)
        blocks: list[dict[str, Any]]
        if real is not None:
            blocks = [self._file(real, label, mime if isinstance(mime, str) else None)]
        elif uri.startswith(("https://", "http://")):
            blocks = [{"type": "resource_link", "uri": uri, "name": label}]
        else:
            log.info("left out attachment %r: %s is not a file this host serves", label, uri)
            return []
        selection = _selection(attachment.get("selection"))
        if selection is not None:
            blocks.append(text(f"(In {label}, the user selected {selection}.)"))
        return blocks

    def _file(self, real: Path, label: str, mime: str | None) -> dict[str, Any]:
        link: dict[str, Any] = {"type": "resource_link", "uri": real.as_uri(), "name": label}
        if mime:
            link["mimeType"] = mime
        if not self.embedded or not real.is_file():
            return link
        try:
            size = real.stat().st_size
            if size > MAX_EMBED:
                return {**link, "size": size}
            body = real.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return link  # binary, or unreadable: the agent can open the link
        resource: dict[str, Any] = {"uri": real.as_uri(), "text": body}
        if mime:
            resource["mimeType"] = mime
        return {"type": "resource", "resource": resource}

    def _inline(self, attachment: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
        data, mime = attachment.get("data"), attachment.get("contentType")
        if not isinstance(data, str) or not isinstance(mime, str):
            return []
        if mime.startswith("image/") and self._image:
            return [{"type": "image", "data": data, "mimeType": mime}]
        if mime.startswith("audio/") and self._audio:
            return [{"type": "audio", "data": data, "mimeType": mime}]
        if not self.embedded:
            log.info("left out attachment %r (%s): the agent takes no embedded data", label, mime)
            return []
        uri = f"attachment:{label}"
        try:
            body = base64.b64decode(data, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return [{"type": "resource", "resource": {"uri": uri, "mimeType": mime, "blob": data}}]
        return [{"type": "resource", "resource": {"uri": uri, "mimeType": mime, "text": body}}]


def _selection(raw: Any) -> str | None:
    """`TextSelection` as words: "lines 3-7" (one-based, as an editor shows)."""
    rng = raw.get("range") if isinstance(raw, Mapping) else None
    start = rng.get("start") if isinstance(rng, Mapping) else None
    end = rng.get("end") if isinstance(rng, Mapping) else None
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        return None
    first, last = start.get("line"), end.get("line")
    if not isinstance(first, int) or not isinstance(last, int):
        return None
    return f"line {first + 1}" if first == last else f"lines {first + 1}-{last + 1}"
