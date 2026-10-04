"""Claude Code's background tasks, as AHP chat background work (1.0.0).

Claude Code reports every task it tracks as `system` messages:
`task_started`, `task_progress`, `task_notification` and `task_updated`. Not
every task is *background* work, and not every one can be shown:

* **`local_bash`** is a shell left running -- `Bash` with `run_in_background`,
  or a command moved to the background mid-run. It becomes a ``shell`` entry.
  Its command line comes from the `Bash` call that started it; the task itself
  only carries the call's description.
* Subagents (`local_agent`, `remote_agent`, `in_process_teammate`) would be
  ``subagent`` entries, but that kind requires the subagent's own chat and this
  adapter does not give subagents chats -- their tool calls are shown inline
  in the parent turn. Publishing one without a chat would be a schema
  violation every client receives, so they are not published.
* Anything else (`local_workflow`, `monitor_mcp`, `dream`, ...) is left alone.

A task ends on a terminal status from **either** `task_notification` or
`task_updated`: the SDK documents that a stopped task may report only the
latter. A task that was started in the foreground and moved to the background
later reports `is_backgrounded` in a `task_updated` patch, with no second
`task_started` -- so a started task is remembered until it ends, whether or
not it was published.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from ahp_host.provider.base import BackgroundWork

__all__ = ["SHELL_TASK", "TERMINAL_STATUSES", "Task", "backgrounded", "now_iso", "work_for"]

#: The task type of a shell running in the background.
SHELL_TASK: Final = "local_bash"

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


def work_for(task: Task) -> BackgroundWork | None:
    """The background work entry for *task*, or ``None`` if it gets none."""
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
