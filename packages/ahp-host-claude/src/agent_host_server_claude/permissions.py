"""Which tool calls run freely, and which a human approves first.

A session picks one of four approval modes when it is created, as the
`permissionMode` session config property. The name and values are Claude
Code's own, which is also what makes VS Code draw its icons for them (shield,
pencil, sparkle, lightbulb); the labels a person reads are Ask, Accept
edits, Auto and Plan.

- ``default``, labelled Ask: "read freely, ask to change". Tools that only look at
  the workspace run without asking; anything that edits, executes, or reaches
  the network is put to the user through the host's `confirm_tool_call`, which
  a client such as VS Code renders as an approval prompt.
- ``acceptEdits``, labelled Accept edits: Claude Code's own mode of that name. File edits in the
  working directory run without asking; shell and web still ask.
- ``auto``, labelled Auto: Claude Code's auto mode. Its classifier approves actions it judges
  safe and refuses risky ones; only what it cannot decide reaches the user.
- ``plan``, labelled Plan: Claude Code's plan mode. Claude researches without
  changing anything, then presents a plan (the ``ExitPlanMode`` tool), which
  the user approves or rejects. Approving it drops the session to ``default``,
  so the work that follows is still approved call by call.

``default`` is enforced with a ``PreToolUse`` hook, not with ``allowed_tools``,
because the Agent SDK consults the user's own Claude Code settings first: an
``allow`` rule in ``~/.claude/settings.json`` would otherwise run a command
without ever reaching the approval prompt. In ``default`` the hook answers ``ask``
for every tool outside the read-only set, which sends the call to
``can_use_tool`` no matter what the settings files allow. In the other two
modes the hook has no opinion on those tools, so Claude Code's mode - and the
user's settings - decide, and anything still undecided comes to the client.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Final

#: Tools that cannot change anything outside the agent's own bookkeeping.
#: `Agent`/`Task` start a sub-agent, whose own tool calls pass this same gate.
READ_ONLY_TOOLS: Final = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "NotebookRead",
        "TodoWrite",
        "Task",
        "Agent",
        "ToolSearch",
    }
)

#: Claude Code's interactive question tool has no host-side renderer yet, so it
#: is withheld and the agent asks in plain text instead.
DISALLOWED_TOOLS: Final = ("AskUserQuestion",)


#: The session config property. VS Code recognises this name and its values.
CONFIG_KEY: Final = "permissionMode"

ASK: Final = "default"
#: What the setting was called before it took Claude Code's names.
_LEGACY_ASK: Final = "ask"
ACCEPT_EDITS: Final = "acceptEdits"
AUTO: Final = "auto"
PLAN: Final = "plan"
#: Claude Code's tool for "here is my plan; may I start?".
EXIT_PLAN_TOOL: Final = "ExitPlanMode"

#: Approval mode -> Claude Code permission mode.
PERMISSION_MODES: Final[Mapping[str, str]] = {
    ASK: "default",
    ACCEPT_EDITS: "acceptEdits",
    AUTO: "auto",
    PLAN: "plan",
}

#: The `approvals` property of the session config schema a client renders.
APPROVALS_PROPERTY: Final[Mapping[str, Any]] = {
    "type": "string",
    "title": "Approvals",
    "description": (
        "Ask: reads run freely, everything else asks you. "
        "Accept edits: file edits in the workspace run without asking. "
        "Auto: Claude Code's classifier approves safe actions and blocks risky ones. "
        "Plan: Claude plans without changing anything, then asks to start."
    ),
    "enum": [ASK, ACCEPT_EDITS, AUTO, PLAN],
    "enumLabels": ["Ask", "Accept edits", "Auto", "Plan"],
    "default": ASK,
}


def approval_mode(value: Any) -> str:
    """A client's `permissionMode` value, or the strict default for anything else."""
    if value == _LEGACY_ASK:
        return ASK
    return value if isinstance(value, str) and value in PERMISSION_MODES else ASK


def needs_approval(tool_name: str) -> bool:
    return tool_name not in READ_ONLY_TOOLS


def pre_tool_use_decision(tool_name: str, mode: str = ASK) -> dict[str, Any]:
    """The hook's answer.

    The read-only set is always allowed. In ``default`` everything else goes to a
    human; in the looser modes the hook stays out of it and Claude Code's
    permission mode decides.
    """
    if needs_approval(tool_name) and mode != ASK:
        return {}
    if needs_approval(tool_name):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "This host asks before any tool that changes things.",
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


def _short(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _name_of(path: Any) -> str:
    return os.path.basename(str(path)) or str(path)


def describe(tool_name: str, tool_input: Mapping[str, Any]) -> tuple[str, str]:
    """(display name, one-line invocation message) for a tool call.

    What a client shows in the tool row and in the approval prompt; the raw
    input travels alongside it, so this only has to be readable.
    """
    match tool_name:
        case "Bash":
            return "Run command", _short(tool_input.get("command", ""))
        case "Read":
            return "Read file", _name_of(tool_input.get("file_path", ""))
        case "Edit" | "MultiEdit":
            return "Edit file", _name_of(tool_input.get("file_path", ""))
        case "Write":
            return "Write file", _name_of(tool_input.get("file_path", ""))
        case "NotebookEdit":
            return "Edit notebook", _name_of(tool_input.get("notebook_path", ""))
        case "Grep":
            return "Search", _short(tool_input.get("pattern", ""))
        case "Glob":
            return "Find files", _short(tool_input.get("pattern", ""))
        case "WebFetch":
            return "Fetch URL", _short(tool_input.get("url", ""))
        case "WebSearch":
            return "Web search", _short(tool_input.get("query", ""))
        case "Task" | "Agent":
            return "Sub-agent", _short(tool_input.get("description", ""))
        case "TodoWrite":
            return "Update plan", "Update the task list"
        case "ExitPlanMode":
            return "Start on the plan", "Approve the plan above to let Claude start"
    return tool_name, tool_name


def progress_line(tool_name: str, tool_input: Mapping[str, Any]) -> str:
    """The line under a running call's name.

    Without one, the host falls back to "Running {display name}", which for
    every shell command reads "Running Run command". Claude gives each Bash
    call a short `description` of its purpose; that is the most useful line a
    person can read, and the command itself is in the input beside it.
    """
    if tool_name == "Bash":
        description = tool_input.get("description")
        if isinstance(description, str) and description.strip():
            return _short(description)
    display, message = describe(tool_name, tool_input)
    return message if message == display else f"{display}: {message}"


def past_tense(tool_name: str, tool_input: Mapping[str, Any], *, failed: bool) -> str:
    """What a finished call did, in place of a bare "Done"/"Failed"."""
    _, message = describe(tool_name, tool_input)
    if failed:
        return f"Failed: {progress_line(tool_name, tool_input)}"
    match tool_name:
        case "Bash":
            return f"Ran `{_short(tool_input.get('command', ''), 80)}`"
        case "Read":
            return f"Read {message}"
        case "Edit" | "MultiEdit" | "NotebookEdit":
            return f"Edited {message}"
        case "Write":
            return f"Wrote {message}"
        case "Grep":
            return f"Searched for {message}"
        case "Glob":
            return f"Found files matching {message}"
        case "WebFetch":
            return f"Fetched {message}"
        case "WebSearch":
            return f"Searched the web for {message}"
        case "Task" | "Agent":
            return f"Sub-agent finished: {message}"
        case "TodoWrite":
            return "Updated the task list"
    return f"Ran {tool_name}"
