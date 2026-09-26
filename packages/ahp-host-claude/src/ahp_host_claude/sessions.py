"""This machine's other Claude Code conversations, to continue from.

Every Claude Code session on a machine - started in a terminal, in the IDE,
driven from a phone through Remote Control, or through this host - is stored
under `~/.claude/projects`, and the Agent SDK can list, read and fork them.
That makes them offerable as a starting point: a new AHP session that
continues one of them with its full context.

Continuing always **forks**. The original may still be open somewhere (a
terminal, a Remote Control session), and two processes resuming the same
session interleave their messages into one transcript; a fork leaves the
original exactly as it was.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeGuard

from claude_agent_sdk import (
    SDKSessionInfo,
    fork_session,
    get_session_info,
    get_session_messages,
    list_sessions,
)

from ahp_host_claude.roots import Roots, as_roots

#: The `continueFrom` value meaning "a fresh conversation".
NEW: Final = "new"
#: How many recent sessions the picker shows before the user types a query.
SEED_COUNT: Final = 8
#: How many a search returns.
SEARCH_LIMIT: Final = 30
#: The recap posted before the first reply of a continued session.
RECAP_CHARS: Final = 600

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_session_id(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def title_of(info: SDKSessionInfo) -> str:
    title = info.custom_title or info.summary or info.first_prompt or info.session_id
    title = " ".join(title.split())
    return title if len(title) <= 70 else title[:69] + "…"


def _age(ms: int, now: float) -> str:
    seconds = max(0, now - ms / 1000)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def describe_session(info: SDKSessionInfo, now: float | None = None) -> str:
    """The line under a picker entry: where it was, and when."""
    parts = []
    if info.cwd:
        parts.append(Path(info.cwd).name or info.cwd)
    if info.git_branch:
        parts.append(info.git_branch)
    parts.append(_age(info.last_modified, time.time() if now is None else now))
    return " · ".join(parts)


def _inside(path: str | None, roots: Roots) -> bool:
    return path is not None and path != "" and roots.contains(Path(path))


def _matches(info: SDKSessionInfo, query: str) -> bool:
    haystack = " ".join(
        part
        for part in (info.custom_title, info.summary, info.first_prompt, info.git_branch, info.cwd)
        if part
    )
    return all(word in haystack.lower() for word in query.lower().split())


def _last_reply(messages: Sequence[Any]) -> str:
    for message in reversed(messages):
        if getattr(message, "type", None) != "assistant":
            continue
        content = (getattr(message, "message", None) or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            if text:
                return text
    return ""


@dataclass(frozen=True)
class Continuation:
    """A fork, ready to resume: the new session id, its folder, and a recap."""

    session_id: str
    directory: Path
    recap: str


class ClaudeCodeSessions:
    """The catalogue, confined to the served folders. SDK calls are injectable for tests."""

    def __init__(
        self,
        root: Path | Roots,
        *,
        list_fn: Callable[..., list[SDKSessionInfo]] = list_sessions,
        info_fn: Callable[..., SDKSessionInfo | None] = get_session_info,
        fork_fn: Callable[..., Any] = fork_session,
        messages_fn: Callable[..., list[Any]] = get_session_messages,
    ) -> None:
        self.roots = as_roots(root)
        self.root = self.roots.primary
        self._list = list_fn
        self._info = info_fn
        self._fork = fork_fn
        self._messages = messages_fn

    async def recent(self, query: str = "", limit: int = SEED_COUNT) -> list[SDKSessionInfo]:
        """Newest first, only those whose folder is inside the root."""
        # Reading the catalogue stats files and reads their heads: off the loop.
        found = await asyncio.to_thread(self._list)
        inside = [info for info in found if _inside(info.cwd, self.roots)]
        if query.strip():
            inside = [info for info in inside if _matches(info, query)]
        inside.sort(key=lambda info: info.last_modified, reverse=True)
        return inside[:limit]

    async def continue_from(self, session_id: str) -> Continuation:
        """Fork `session_id` and describe the fork. Refuses anything outside the root."""
        if not is_session_id(session_id):
            raise ValueError(f"not a Claude Code session id: {session_id!r}")
        info = await asyncio.to_thread(self._info, session_id)
        if info is None:
            raise FileNotFoundError(f"no Claude Code session {session_id} on this machine")
        if not _inside(info.cwd, self.roots):
            raise PermissionError(f"session {session_id} ran outside this host's root {self.root}")
        assert info.cwd is not None  # _inside is False without one
        forked = await asyncio.to_thread(
            self._fork, session_id, directory=info.cwd, title=f"{title_of(info)} (continued)"
        )
        messages = await asyncio.to_thread(self._messages, session_id, directory=info.cwd)
        reply = _last_reply(messages)
        if len(reply) > RECAP_CHARS:
            reply = reply[: RECAP_CHARS - 1] + "…"
        recap = (
            f"*Continuing “{title_of(info)}” ({describe_session(info)}), forked from "
            f"Claude Code session `{session_id}`. Claude has the whole conversation.*"
        )
        if reply:
            quoted = "\n".join(f"> {line}" if line else ">" for line in reply.splitlines())
            recap += f"\n\nLast reply:\n\n{quoted}"
        return Continuation(forked.session_id, Path(info.cwd).resolve(), recap + "\n\n---\n\n")

    def picker_property(self, recent: Sequence[SDKSessionInfo]) -> dict[str, Any]:
        """The `continueFrom` session config property, seeded with `recent`."""
        now = time.time()
        return {
            "type": "string",
            "title": "Continue from",
            "description": (
                "Start fresh, or continue one of this machine's Claude Code conversations "
                "(terminal, IDE or Remote Control). The original is left untouched: "
                "this continues a copy."
            ),
            "enum": [NEW, *(info.session_id for info in recent)],
            "enumLabels": ["New conversation", *(title_of(info) for info in recent)],
            "enumDescriptions": [
                "A fresh conversation",
                *(describe_session(info, now) for info in recent),
            ],
            "enumDynamic": True,
            "default": NEW,
        }
