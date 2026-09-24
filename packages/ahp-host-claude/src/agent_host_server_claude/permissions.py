"""Which tool calls run freely, and which a human approves first.

The policy is "read freely, ask to change": tools that only look at the
workspace run without asking; anything that edits, executes, or reaches the
network is put to the user through the host's `confirm_tool_call`, which a
client such as VS Code renders as an approval prompt.

It is enforced with a ``PreToolUse`` hook, not with ``allowed_tools``, because
the Agent SDK consults the user's own Claude Code settings first: an
``allow`` rule in ``~/.claude/settings.json`` would otherwise run a command
without ever reaching the approval prompt. The hook answers ``ask`` for every
tool outside the read-only set, which sends the call to ``can_use_tool`` no
matter what the settings files allow.
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


def needs_approval(tool_name: str) -> bool:
    return tool_name not in READ_ONLY_TOOLS


def pre_tool_use_decision(tool_name: str) -> dict[str, Any]:
    """The hook's answer: allow the read-only set, send the rest to a human."""
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
    return tool_name, tool_name
