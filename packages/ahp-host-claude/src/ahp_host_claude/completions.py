"""``/`` and ``@`` completions for a message being typed (`completions`).

Two things Claude Code itself completes in its own prompt, and both map
cleanly onto a completion item with an attachment:

* ``/`` at the start of a message: Claude Code's slash commands and skills,
  the user-invocable ones it lists for this session (`get_server_info()`
  ``commands``, refreshed by `system/commands_changed`), minus those bound to
  its terminal (`system/init` ``terminal_slash_commands``). Only at the start,
  because that is the only place Claude Code runs one. The item inserts
  ``/name `` and carries a ``simple`` attachment marked
  ``_meta.claudeCode.command``: the command is in the message text already,
  so `attachments.py` adds nothing for it.
* ``@`` anywhere: files and folders, from Claude Code's own file index
  (`file_suggestions`, the fuzzy, ignore-aware matcher its prompt uses). The
  item inserts ``@path`` - Claude Code's own mention, which it reads - and
  carries a ``resource`` attachment with the file's URI as clients see it.
  Only paths inside the served folders are offered.

Every attachment has a ``label``, which the spec requires and is what a picker
shows.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ahp_host.provider.base import CompletionItem, CompletionRequest

from ahp_host_claude.roots import Roots

__all__ = [
    "COMMAND_META",
    "TRIGGERS",
    "Token",
    "file_items",
    "is_command",
    "slash_items",
    "token_of",
]

#: What the host should advertise as `completionTriggerCharacters`.
TRIGGERS: Final = ("/", "@")
#: The `_meta` key a slash command's attachment carries.
COMMAND_META: Final = "claudeCode"
#: Enough to choose from; a picker scrolls no further than this anyway.
LIMIT: Final = 50

_TOKEN = re.compile(r"(?:^|(?<=\s))([/@])(\S*)$")


@dataclass(frozen=True)
class Token:
    """The completable word before the cursor."""

    trigger: str
    typed: str
    #: Where it starts in the input, in UTF-16 code units.
    start: int
    #: Whether only whitespace comes before it.
    leading: bool


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def token_of(request: CompletionRequest) -> Token | None:
    prefix = request.text_before_cursor()
    match = _TOKEN.search(prefix)
    if match is None:
        return None
    before = prefix[: match.start(1)]
    return Token(
        trigger=match.group(1),
        typed=match.group(2),
        start=_utf16(before),
        leading=not before.strip(),
    )


def is_command(attachment: Mapping[str, Any]) -> bool:
    """A slash command's attachment: nothing to add to the prompt."""
    meta = attachment.get("_meta")
    marker = meta.get(COMMAND_META) if isinstance(meta, Mapping) else None
    return isinstance(marker, Mapping) and isinstance(marker.get("command"), str)


def slash_items(
    token: Token,
    end: int,
    commands: Sequence[Mapping[str, Any]],
    *,
    hidden: Collection[str] = (),
) -> list[CompletionItem]:
    if token.trigger != "/" or not token.leading:
        return []
    typed = token.typed.casefold()
    ranked: list[tuple[int, str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for command in commands:
        name = command.get("name")
        if not isinstance(name, str) or not name or name in hidden or name in seen:
            continue
        folded = name.casefold()
        short = folded.split(":", 1)[-1]
        if folded.startswith(typed):
            rank = 0
        elif short.startswith(typed):
            rank = 1
        elif typed and typed in folded:
            rank = 2
        else:
            continue
        seen.add(name)
        ranked.append((rank, folded, command))
    ranked.sort(key=lambda item: (item[0], item[1]))
    items: list[CompletionItem] = []
    for _, _, command in ranked[:LIMIT]:
        name = str(command["name"])
        marker: dict[str, Any] = {"command": name}
        for key in ("description", "argumentHint"):
            value = command.get(key)
            if isinstance(value, str) and value:
                marker[key] = value
        items.append(
            CompletionItem(
                insert_text=f"/{name} ",
                range_start=token.start,
                range_end=end,
                attachment={
                    "type": "simple",
                    "label": f"/{name}",
                    "displayKind": "command",
                    "_meta": {COMMAND_META: marker},
                },
            )
        )
    return items


def _mention(path: str) -> str:
    return f'@"{path}" ' if any(c.isspace() for c in path) else f"@{path} "


def file_items(
    token: Token,
    end: int,
    reply: Mapping[str, Any],
    *,
    cwd: Path,
    roots: Roots,
) -> list[CompletionItem]:
    """Claude Code's `file_suggestions` answer as items, inside the served folders."""
    if token.trigger != "@":
        return []
    base = reply.get("cwd")
    root = Path(base) if isinstance(base, str) and base else cwd
    suggestions = reply.get("suggestions")
    items: list[CompletionItem] = []
    for suggestion in suggestions if isinstance(suggestions, Sequence) else ():
        path = suggestion.get("path") if isinstance(suggestion, Mapping) else None
        if not isinstance(path, str) or not path:
            continue
        folder = path.endswith(("/", "\\"))
        real = (root / path).resolve()
        uri = roots.tree_uri(real)
        if uri is None:
            continue  # outside every served folder: not this host's to offer
        shown = path.rstrip("/\\")
        label = (Path(shown).name or shown) + ("/" if folder else "")
        items.append(
            CompletionItem(
                insert_text=_mention(shown),
                range_start=token.start,
                range_end=end,
                attachment={
                    "type": "resource",
                    "uri": uri,
                    "label": label,
                    "displayKind": "directory" if folder else "document",
                },
            )
        )
        if len(items) >= LIMIT:
            break
    return items
