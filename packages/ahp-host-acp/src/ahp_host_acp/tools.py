"""How an ACP tool call reads in a client's tool row.

ACP names no tools: a call has a free-text `title`, a `kind` from a small
fixed set (`read`, `edit`, `delete`, `move`, `search`, `execute`, `think`,
`fetch`, `switch_mode`, `other`), optional `locations`, and the agent's own
`rawInput`. The kind picks the row's name and verb; the rest fills in what it
acted on.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

_DISPLAY: Final[Mapping[str, str]] = {
    "read": "Read",
    "edit": "Edit",
    "delete": "Delete",
    "move": "Move",
    "search": "Search",
    "execute": "Run command",
    "think": "Think",
    "fetch": "Fetch",
    "switch_mode": "Switch mode",
}

_PAST: Final[Mapping[str, str]] = {
    "read": "Read",
    "edit": "Edited",
    "delete": "Deleted",
    "move": "Moved",
    "search": "Searched",
    "fetch": "Fetched",
    "think": "Thought",
    "switch_mode": "Switched mode",
}


def _guess_kind(raw_input: Any) -> str:
    """A kind for an agent that sends none (goose), from the shape of its input.

    Only the unambiguous shapes: a `command` is a shell call; a `path` with
    `content` writes a file. Anything else stays `other`.
    """
    if not isinstance(raw_input, Mapping):
        return "other"
    if isinstance(raw_input.get("command"), str):
        return "execute"
    if isinstance(raw_input.get("path"), str) and "content" in raw_input:
        return "edit"
    return "other"


def _short(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class ToolCall:
    """One call as the agent has described it so far; updates merge in."""

    call_id: str
    title: str = ""
    kind: str = "other"
    raw_input: Any = None
    locations: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    content: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    status: str = "pending"

    def merge(self, update: Mapping[str, Any]) -> None:
        """Fold in a `tool_call` or `tool_call_update`: absent fields keep their value."""
        if isinstance(title := update.get("title"), str) and title:
            self.title = title
        if isinstance(kind := update.get("kind"), str) and kind:
            self.kind = kind
        if "rawInput" in update and update["rawInput"] is not None:
            self.raw_input = update["rawInput"]
        if isinstance(locations := update.get("locations"), list):
            self.locations = [loc for loc in locations if isinstance(loc, Mapping)]
        if isinstance(content := update.get("content"), list):
            self.content = [item for item in content if isinstance(item, Mapping)]
        if isinstance(status := update.get("status"), str) and status:
            self.status = status
        if self.kind == "other":
            self.kind = _guess_kind(self.raw_input)

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed")

    @property
    def name(self) -> str:
        return self.kind or "other"

    @property
    def display_name(self) -> str:
        return _DISPLAY.get(self.kind) or _short(self.title or "Tool", 60)

    def _input(self, key: str) -> str | None:
        if isinstance(self.raw_input, Mapping):
            value = self.raw_input.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    def _target(self) -> str:
        """What the call acts on: a file's name, a command, else the title."""
        for location in self.locations:
            path = location.get("path")
            if isinstance(path, str) and path:
                return os.path.basename(path) or path
        if path := self._input("path"):
            return os.path.basename(path) or path
        return _short(self._input("command") or self.title or self.kind, 80)

    def progress_line(self) -> str:
        """The line under a running call's name.

        Some agents put their own one-line purpose in the input (OpenClaw's
        `exec` has `title`: "Run echo hi"); that reads best. Otherwise the
        target, which for a shell command is the command itself.
        """
        return _short(self._input("description") or self._input("title") or self._target())

    def approval_line(self) -> str:
        """What the approval prompt says: for a shell command, the command itself,
        since that is what is being approved; otherwise the progress line."""
        command = self._input("command")
        if self.kind == "execute" and command:
            return _short(command, 200)
        return self.progress_line()

    def past_tense(self) -> str:
        if self.status == "failed":
            return f"Failed: {self.progress_line()}"
        if self.kind == "execute":
            command = self._input("command")
            return f"Ran `{_short(command, 80)}`" if command else f"Ran {self._target()}"
        verb = _PAST.get(self.kind)
        return f"{verb} {self._target()}" if verb else f"Done: {self.progress_line()}"


def text_of(content: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """ACP tool-call content as the host's text parts.

    `content` blocks carry their text; a `diff` becomes a unified-looking
    summary (the host has no diff part); `terminal` ids mean nothing without
    ACP's terminal methods, which this client does not offer.
    """
    parts: list[dict[str, Any]] = []
    for item in content:
        kind = item.get("type")
        if kind == "content":
            block = item.get("content")
            if isinstance(block, Mapping) and block.get("type") == "text":
                parts.append({"type": "text", "text": str(block.get("text", ""))})
            elif isinstance(block, Mapping) and block.get("type") == "resource_link":
                parts.append({"type": "text", "text": str(block.get("uri", ""))})
        elif kind == "diff":
            path = str(item.get("path", ""))
            new = str(item.get("newText", ""))
            old = item.get("oldText")
            header = f"--- {path}\n+++ {path}\n" if old is not None else f"+++ {path} (new)\n"
            parts.append({"type": "text", "text": header + new})
    return parts
