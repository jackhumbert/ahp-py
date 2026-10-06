"""Which tool calls run freely, and which a human approves first.

A session picks one of four approval modes when it is created, as the
`permissionMode` session config property, and may switch between them later.
The name and values are Claude Code's own, which is also what makes VS Code
draw its icons for them (shield, pencil, sparkle, lightbulb); the labels a
person reads are Ask, Accept edits, Auto and Plan.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ahp_host.provider.base import ConfirmationOption, ToolConfirmationOutcome
from claude_agent_sdk import PermissionUpdate
from claude_agent_sdk.types import PermissionRuleValue

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

#: Claude Code's multiple-choice question tool. It is answered through the
#: host's input request (`chat/inputRequested`, `questions.py`), so the agent
#: may use it; until that existed it was withheld here and Claude asked in
#: plain text instead.
QUESTION_TOOL: Final = "AskUserQuestion"

#: Tools Claude is never offered. None today: the last one, the question tool,
#: now has a renderer.
DISALLOWED_TOOLS: Final[tuple[str, ...]] = ()


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
    # Changeable during a session: the host tells the session
    # (`ClaudeSession.config_changed`) and it switches the running client.
    "sessionMutable": True,
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

    The question tool always goes to the approval callback, in every mode: that
    callback is where its questions are put to a person and their answers come
    back. Anything that allowed it past the callback - an ``allow`` rule in the
    user's settings - would run it with no answers at all.
    """
    if tool_name == QUESTION_TOOL:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "Questions are put to the user.",
            }
        }
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
        case "AskUserQuestion":
            return "Ask you", _short(_first_question(tool_input) or "Questions for you")
    return tool_name, tool_name


def _first_question(tool_input: Mapping[str, Any]) -> str | None:
    questions = tool_input.get("questions")
    if isinstance(questions, list) and questions and isinstance(questions[0], Mapping):
        text = questions[0].get("question")
        return text if isinstance(text, str) and text else None
    return None


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
        case "AskUserQuestion":
            return f"Asked: {message}"
    return f"Ran {tool_name}"


# -- the choices on an approval prompt ------------------------------------------
#
# Security-relevant. Claude Code suggests, with each prompt, what would let a
# call like this one through next time (`ToolPermissionContext.suggestions`):
# an allow rule, a looser mode, another directory. They are offered as options
# on the prompt, and one is applied only when the user picks it - never by
# default, and never anything broader than the option says:
#
# * every suggestion is narrowed to this session (`destination: session`): a
#   client here never writes the user's Claude Code settings files, which every
#   other Claude Code on the machine reads;
# * an allow rule is offered only outside Ask, because in Ask the `PreToolUse`
#   gate puts every changing call to the user whatever the rules say - the
#   option would not do what it says;
# * a mode is offered only if this adapter offers it (Accept edits, Auto):
#   never bypass permissions;
# * a directory is never offered: what a session may touch is its working
#   directories, inside the served folders.

#: The id of the plain "allow this call" option, and of the plain denial.
ALLOW_ONCE: Final = "allow"
DENY: Final = "deny"
#: Prefix of an option that applies one of Claude Code's suggestions.
SUGGESTION: Final = "suggestion:"

_MODE_OPTIONS: Final[Mapping[str, str]] = {
    ACCEPT_EDITS: "Allow, and accept file edits from now on (Accept edits)",
    AUTO: "Allow, and let Claude Code's classifier decide from now on (Auto)",
}


@dataclass(frozen=True)
class Choices:
    """The options on one prompt, and what each suggestion option applies."""

    options: tuple[ConfirmationOption, ...] = ()
    updates: Mapping[str, PermissionUpdate] = field(default_factory=dict)

    def chosen(self, outcome: ToolConfirmationOutcome) -> PermissionUpdate | None:
        """The update the user explicitly picked, if any."""
        picked = outcome.selected_option_id
        if not outcome.approved or picked is None:
            return None
        return self.updates.get(picked)


def _rule_text(rule: PermissionRuleValue) -> str:
    return f"{rule.tool_name}({rule.rule_content})" if rule.rule_content else rule.tool_name


def _narrowed(suggestion: Any, mode: str) -> tuple[str, PermissionUpdate] | None:
    """A suggestion as an option label and the session-only update it applies."""
    if not isinstance(suggestion, PermissionUpdate):
        return None
    if suggestion.type == "addRules" and suggestion.behavior == "allow" and suggestion.rules:
        if mode == ASK:
            return None
        rules = [rule for rule in suggestion.rules if isinstance(rule, PermissionRuleValue)]
        if not rules:
            return None
        label = ", ".join(f"`{_rule_text(rule)}`" for rule in rules)
        update = PermissionUpdate(
            type="addRules", rules=rules, behavior="allow", destination="session"
        )
        return f"Allow {label} for this session", update
    if suggestion.type == "setMode" and suggestion.mode in _MODE_OPTIONS:
        target = str(suggestion.mode)
        if target == mode:
            return None
        update = PermissionUpdate(type="setMode", mode=suggestion.mode, destination="session")
        return _MODE_OPTIONS[target], update
    return None


def choices(suggestions: Sequence[Any], mode: str) -> Choices:
    """The options for a prompt: allow once, each usable suggestion, deny.

    None at all when no suggestion is usable, so the prompt stays a plain
    approve/deny - the spec's "render these instead" would otherwise replace
    it with the same two buttons.
    """
    offered: list[ConfirmationOption] = []
    updates: dict[str, PermissionUpdate] = {}
    seen: set[str] = set()
    for index, suggestion in enumerate(suggestions):
        narrowed = _narrowed(suggestion, mode)
        if narrowed is None or narrowed[0] in seen:
            continue
        label, update = narrowed
        seen.add(label)
        identifier = f"{SUGGESTION}{index}"
        offered.append(ConfirmationOption(id=identifier, label=label, kind="approve", group=1))
        updates[identifier] = update
    if not offered:
        return Choices()
    return Choices(
        options=(
            ConfirmationOption(id=ALLOW_ONCE, label="Allow", kind="approve", group=1),
            *offered,
            ConfirmationOption(id=DENY, label="Deny", kind="deny", group=2),
        ),
        updates=updates,
    )


def denial_message(outcome: ToolConfirmationOutcome) -> str:
    """What Claude reads when the user said no: why, and what to do instead.

    A denial without its reason is an agent that tries the same thing again.
    A suggestion's text is passed on as the user wrote it; anything attached
    to it is not, and the message says so rather than implying it was read.
    """
    if outcome.reason == "skipped":
        parts = ["The user skipped this tool call."]
    else:
        parts = ["The user declined this tool call."]
    reason = (outcome.reason_message or "").strip()
    if reason:
        parts.append(f"Their reason: {reason}")
    suggestion = outcome.user_suggestion
    text = suggestion.text.strip() if suggestion is not None else ""
    if text:
        parts.append(f"What they would like instead: {text}")
        attachments = suggestion.raw.get("attachments") if suggestion is not None else None
        if isinstance(attachments, Sequence) and not isinstance(attachments, str) and attachments:
            parts.append("(They attached something to that suggestion, which is not shown here.)")
    return " ".join(parts)
