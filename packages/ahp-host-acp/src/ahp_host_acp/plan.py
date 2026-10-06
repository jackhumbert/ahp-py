"""An ACP agent's plan, as a tool-call row a client can open.

ACP's `plan` update (stable) is the agent's task list: entries with
`content`, `priority` (`high` | `medium` | `low`) and `status` (`pending` |
`in_progress` | `completed`), and "the Client MUST replace the current plan
completely" on each one.

AHP 1.0.0 has nothing plan- or todo-shaped: no chat or session state for it,
no response-part kind. The candidates, and why this is the one:

- **A markdown response part, rewritten in place** -- the closest to ACP's
  "replace completely", but a provider cannot rewrite one: the turn sink only
  appends (`text_delta`), so the plan would be pasted into the agent's prose
  again on every change, as if the agent had said it.
- **A tool-call row per plan update** -- what this does. "Update plan", then
  "Updated the plan: 2 of 5 done", with the whole list as its result. It is
  honest about what happened (the agent revised its plan, at that point in the
  turn), it never mixes into the answer, and it is exactly how the same thing
  reads through the Claude host, whose `TodoWrite` rows say "Update plan" /
  "Updated the task list". An unchanged plan sent again adds no row.

The entry being worked on also becomes the session's activity line, so the
session list says what the agent is doing rather than the client's fallback.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: The row's tool name and what a client shows for it.
TOOL_NAME: Final = "plan"
DISPLAY_NAME: Final = "Update plan"


@dataclass(frozen=True)
class Entry:
    content: str
    status: str = "pending"
    priority: str = "medium"


def parse_plan(raw: Any) -> tuple[Entry, ...]:
    """`Plan.entries`, skipping malformed ones."""
    entries: list[Entry] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, Mapping) or not isinstance(item.get("content"), str):
            continue
        status, priority = item.get("status"), item.get("priority")
        entries.append(
            Entry(
                content=item["content"],
                status=status if isinstance(status, str) else "pending",
                priority=priority if isinstance(priority, str) else "medium",
            )
        )
    return tuple(entries)


def _line(entry: Entry) -> str:
    text = " ".join(entry.content.split()) or "(untitled)"
    if entry.status == "in_progress":
        text = f"**{text}** (in progress)"
    if entry.priority == "high":
        text += " (high priority)"
    box = "x" if entry.status == "completed" else " "
    return f"- [{box}] {text}"


def markdown(entries: Sequence[Entry]) -> str:
    """The plan as a markdown task list."""
    if not entries:
        return "The plan is empty."
    return "\n".join(_line(entry) for entry in entries)


def progress(entries: Sequence[Entry]) -> str:
    """ "2 of 5 done", for the row's past-tense line."""
    done = sum(1 for entry in entries if entry.status == "completed")
    return f"{done} of {len(entries)} done"


def current(entries: Sequence[Entry]) -> str | None:
    """The entry the agent says it is working on, if any."""
    return next((e.content for e in entries if e.status == "in_progress"), None)
