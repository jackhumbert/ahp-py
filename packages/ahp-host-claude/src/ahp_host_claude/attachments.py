"""A user message's attachments, as Claude Code content.

AHP carries attachments on the wire message (`UserMessage.raw["attachments"]`)
as a union discriminated by `type`:

- `resource`: a URI, not the content. A local file or folder is handed to
  Claude as its path, which it reads with its own (auto-approved) tools; that
  keeps a large file out of the prompt unless Claude decides it needs it.
- `embeddedResource`: base64 data. Images go to Claude as image blocks, PDFs as
  document blocks, and text is decoded inline.
- `simple`: opaque to the host; `modelRepresentation` is the text the producer
  says the model should see. One this host produced for a slash command
  (`completions.py`) adds nothing: the command is already the message's text.
- `chat`: another chat's transcript, which the host resolves
  (`UserMessage.attached_chats`) and Claude gets as text (`transcripts.py`),
  bounded by the attachment's `endTurn`.
- `annotations`: a reference to host state this adapter cannot resolve yet.
  It is named in the prompt rather than dropped silently.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ahp_host.provider.base import AttachedChat

from ahp_host_claude.completions import is_command
from ahp_host_claude.paths import directory_of
from ahp_host_claude.roots import Roots, as_roots
from ahp_host_claude.transcripts import transcript

log = logging.getLogger(__name__)

#: What the Messages API accepts as an image block.
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
#: Inline text is capped so one pasted log cannot fill the context window.
MAX_INLINE_TEXT = 200_000

Block = dict[str, Any]


def _selection(attachment: Mapping[str, Any]) -> str:
    selection = attachment.get("selection")
    if not isinstance(selection, Mapping):
        return ""
    text_range = selection.get("range")
    if not isinstance(text_range, Mapping):
        return ""
    start, end = text_range.get("start"), text_range.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        return ""
    try:
        # The protocol's positions are zero-based; people read lines from one.
        first, last = int(start["line"]) + 1, int(end["line"]) + 1
    except (KeyError, TypeError, ValueError):
        return ""
    return f", lines {first}-{last}" if last != first else f", line {first}"


def _label(attachment: Mapping[str, Any]) -> str:
    label = attachment.get("label")
    return label if isinstance(label, str) and label else "attachment"


def _resource(attachment: Mapping[str, Any], roots: Roots) -> str:
    uri = attachment.get("uri")
    label = _label(attachment)
    resolved = roots.real_path(uri) if isinstance(uri, str) else None
    if resolved is None:
        path = directory_of(uri) if isinstance(uri, str) else None
        if path is None:
            return f"[Attached {label}: {uri} (not a local file; not fetched)]"
        # Same boundary as the working directory: only the served folders.
        return f"[Attached {label}: {path.resolve()} is outside this host's root; not read]"
    kind = "folder" if attachment.get("displayKind") == "directory" else "file"
    return f"[Attached {kind} {label}: {resolved}{_selection(attachment)}]"


def _embedded(attachment: Mapping[str, Any]) -> list[Block]:
    label = _label(attachment)
    content_type = str(attachment.get("contentType", "")).split(";")[0].strip().lower()
    data = attachment.get("data")
    if not isinstance(data, str):
        return [{"type": "text", "text": f"[Attached {label}: no data]"}]
    if content_type in IMAGE_TYPES:
        return [
            {"type": "text", "text": f"[Attached image {label}]"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": content_type, "data": data},
            },
        ]
    if content_type == "application/pdf":
        return [
            {"type": "text", "text": f"[Attached PDF {label}]"},
            {
                "type": "document",
                "source": {"type": "base64", "media_type": content_type, "data": data},
            },
        ]
    try:
        text = base64.b64decode(data, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return [{"type": "text", "text": f"[Attached {label} ({content_type}): binary, not shown]"}]
    truncated = len(text) > MAX_INLINE_TEXT
    body = text[:MAX_INLINE_TEXT]
    note = f" (first {MAX_INLINE_TEXT} characters)" if truncated else ""
    return [
        {
            "type": "text",
            "text": f"[Attached {label}{_selection(attachment)}{note}]\n{body}",
        }
    ]


def _chat(attachment: Mapping[str, Any], resolved: list[AttachedChat]) -> Block:
    """A chat attachment as its transcript: the next resolved one for its chat."""
    label = _label(attachment)
    resource = attachment.get("resource")
    found = next((chat for chat in resolved if chat.resource == resource), None)
    if found is None:
        return {
            "type": "text",
            "text": f"[Attached chat {label}: its transcript was not available]",
        }
    resolved.remove(found)
    text = transcript(found.turns)
    if not text:
        return {"type": "text", "text": f"[Attached chat {label}: it has no turns yet]"}
    return {"type": "text", "text": f"[Attached chat {label}, its transcript:]\n{text}"}


def attachment_blocks(
    attachments: Sequence[Any],
    root: Path | Roots,
    attached_chats: Sequence[AttachedChat] = (),
) -> list[Block]:
    """Content blocks for every attachment, in order. Never raises."""
    blocks: list[Block] = []
    resolved = list(attached_chats)
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            continue
        kind = attachment.get("type")
        if kind == "resource":
            blocks.append({"type": "text", "text": _resource(attachment, as_roots(root))})
        elif kind == "embeddedResource":
            blocks.extend(_embedded(attachment))
        elif kind == "simple":
            if is_command(attachment):
                continue
            text = attachment.get("modelRepresentation")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            else:
                blocks.append({"type": "text", "text": f"[Attached {_label(attachment)}]"})
        elif kind == "chat":
            blocks.append(_chat(attachment, resolved))
        else:
            log.info("attachment type %r is not supported yet", kind)
            blocks.append(
                {
                    "type": "text",
                    "text": f"[Attached {_label(attachment)} ({kind}): not supported by this host]",
                }
            )
    return blocks


def prompt_content(
    text: str,
    raw: Mapping[str, Any],
    root: Path | Roots,
    attached_chats: Sequence[AttachedChat] = (),
) -> str | list[Block]:
    """The prompt for Claude: plain text, or content blocks when there are attachments."""
    attachments = raw.get("attachments")
    if not isinstance(attachments, Sequence) or isinstance(attachments, str) or not attachments:
        return text
    blocks = attachment_blocks(attachments, root, attached_chats)
    if not blocks:
        return text
    return [{"type": "text", "text": text}, *blocks] if text else blocks
