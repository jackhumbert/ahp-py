"""An ACP agent's slash commands, as AHP completions.

ACP's `available_commands_update` (stable) lists the commands an agent
accepts; a client runs one by sending its name after a slash as the prompt's
text, ``/web agent client protocol``. So a completion only has to put that
text in the message: ``/name `` replaces what the user has typed of it, and the
adapter, which sends the message text as the prompt, needs nothing else.

Completions, and not customizations: every AHP customization lives in a
container (an Open Plugins plugin or a watched directory) with a source URI,
and can be toggled by a client. An ACP command has none of those -- the agent
names it and nothing can turn it off -- so publishing commands as children of
an invented plugin would claim a source that does not exist and offer a toggle
the agent would ignore.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ahp_host.provider.base import CompletionItem, CompletionRequest

#: What a client must be told to trigger on (`completionTriggerCharacters`).
TRIGGER: Final = "/"


@dataclass(frozen=True)
class Command:
    """One `AvailableCommand`."""

    name: str
    description: str = ""
    #: `input.hint` for a command that takes text after its name.
    hint: str | None = None


def parse_commands(raw: Any) -> tuple[Command, ...]:
    """`availableCommands`, skipping malformed entries rather than failing."""
    commands: list[Command] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        description = item.get("description")
        given = item.get("input")
        hint = given.get("hint") if isinstance(given, Mapping) else None
        commands.append(
            Command(
                name=name.strip().lstrip("/"),
                description=description if isinstance(description, str) else "",
                hint=hint if isinstance(hint, str) else None,
            )
        )
    return tuple(commands)


def _utf16(text: str) -> int:
    """`len` in UTF-16 code units, the protocol's offset unit."""
    return len(text.encode("utf-16-le")) // 2


def complete(commands: Sequence[Command], request: CompletionRequest) -> list[CompletionItem]:
    """Commands matching a ``/name`` at the very start of the message.

    Only at the start: an agent reads a command from the beginning of the
    prompt, so offering one mid-sentence would insert text that is sent as
    plain prose. And only while the cursor is still in the name -- once a space
    is typed the user is writing the command's input.
    """
    before = request.text_before_cursor()
    stripped = before.lstrip()
    if not stripped.startswith(TRIGGER):
        return []
    typed = stripped[len(TRIGGER) :]
    if any(ch.isspace() for ch in typed):
        return []
    start = _utf16(before[: len(before) - len(stripped)])
    end = _utf16(before)
    wanted = typed.lower()
    return [
        CompletionItem(
            insert_text=f"{TRIGGER}{command.name} ",
            range_start=start,
            range_end=end,
            # A `simple` attachment: the command is in the text itself, which
            # is what the agent reads. `modelRepresentation` "MAY be omitted
            # when the attachment originated from a `completions` response".
            attachment={
                "type": "simple",
                "label": f"{TRIGGER}{command.name}",
                "displayKind": "command",
                "_meta": {"acpCommand": command.name},
            },
        )
        for command in commands
        if command.name.lower().startswith(wanted)
    ]
