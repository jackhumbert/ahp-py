"""Claude Code's background tasks, as AHP chat background work (1.0.0).

Claude Code reports every task it tracks as `system` messages:
`task_started`, `task_progress`, `task_notification` and `task_updated`. Not
every task is *background* work, and not every one can be shown:

* **`local_bash`** is a shell left running -- `Bash` with `run_in_background`,
  or a command moved to the background mid-run. It becomes a ``shell`` entry.
  Its command line comes from the `Bash` call that started it; the task itself
  only carries the call's description.
* A subagent (`local_agent`, `remote_agent`, `in_process_teammate`) running in
  the background gets a worker chat of its own (`open_tool_chat`): its
  messages -- which arrive after the turn that started it has ended -- are
  streamed there, and it becomes a ``subagent`` entry pointing at that chat.
  A subagent in the foreground is unchanged: its tool calls show inline in the
  parent turn, which is still running.
* Anything else (`local_workflow`, `monitor_mcp`, `dream`, ...) is left alone.

A task ends on a terminal status from **either** `task_notification` or
`task_updated`: the SDK documents that a stopped task may report only the
latter. A task that was started in the foreground and moved to the background
later reports `is_backgrounded` in a `task_updated` patch, with no second
`task_started` -- so a started task is remembered until it ends, whether or
not it was published.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from ahp_host.provider.base import BackgroundWork, ProviderChat, TurnSink

__all__ = [
    "SHELL_TASK",
    "SUBAGENT_TASKS",
    "TERMINAL_STATUSES",
    "Subagent",
    "Task",
    "backgrounded",
    "now_iso",
    "work_for",
]

#: The task type of a shell running in the background.
SHELL_TASK: Final = "local_bash"

#: The task types of a subagent.
SUBAGENT_TASKS: Final = frozenset({"local_agent", "remote_agent", "in_process_teammate"})

#: `claude_agent_sdk.TERMINAL_TASK_STATUSES` -- both vocabularies, since
#: `task_notification` says `stopped` where `task_updated` says `killed`.
#: Restated rather than imported because older SDKs lack the constant.
TERMINAL_STATUSES: Final = frozenset({"completed", "failed", "stopped", "killed"})


@dataclass
class Task:
    """What `task_started` said about one task, kept until it ends."""

    task_id: str
    task_type: str | None
    description: str
    started_at: str
    tool_use_id: str | None = None
    #: The command line, when the task came from a `Bash` call this session saw.
    command: str | None = None
    #: Whether Claude Code says it runs in the background. ``None`` when the
    #: CLI did not say, which for a shell task means yes: a shell becomes a
    #: task only by being backgrounded.
    backgrounded: bool | None = None
    published: bool = False
    #: A subagent's type, as Claude Code names it (`Explore`, ...).
    agent_type: str | None = None

    @property
    def work_id(self) -> str:
        return f"{self.task_type or 'task'}:{self.task_id}"


def now_iso() -> str:
    """ISO 8601 with milliseconds and `Z`, the shape AHP timestamps take."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def backgrounded(data: Mapping[str, Any]) -> bool | None:
    """The CLI's `is_backgrounded` flag from a raw task payload or patch."""
    flag = data.get("is_backgrounded", data.get("isBackgrounded"))
    return flag if isinstance(flag, bool) else None


@dataclass
class Subagent:
    """A background subagent's worker chat, and what streams into it."""

    task: Task
    chat: ProviderChat
    #: The worker turn's sink, once the host has started it.
    sink: TurnSink | None = None
    #: Messages that arrived before the sink did, in order.
    pending: list[Any] = field(default_factory=list)
    #: Set when the task ends; the worker turn ends with it.
    done: asyncio.Event = field(default_factory=asyncio.Event)
    announced: set[str] = field(default_factory=set)
    inputs: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    streamed: set[str] = field(default_factory=set)
    current_message: str | None = None


def work_for(task: Task, chat: str | None = None) -> BackgroundWork | None:
    """The background work entry for *task*, or ``None`` if it gets none.

    A subagent's entry needs *chat*, its worker chat.
    """
    if task.task_type in SUBAGENT_TASKS:
        if chat is None or not task.backgrounded:
            return None
        meta: dict[str, Any] = {"taskId": task.task_id}
        if task.agent_type is not None:
            meta["agentType"] = task.agent_type
        return BackgroundWork(
            id=task.work_id,
            kind="subagent",
            label=task.description or task.agent_type or "Subagent",
            started_at=task.started_at,
            chat=chat,
            meta=meta,
        )
    if task.task_type != SHELL_TASK or task.backgrounded is False:
        return None
    label = task.description or task.command or "Background shell"
    return BackgroundWork(
        id=task.work_id,
        kind="shell",
        label=label,
        started_at=task.started_at,
        command=task.command or task.description or label,
        # A background shell dies with the Claude Code process that runs it.
        meta={"attached": True, "taskId": task.task_id},
    )
