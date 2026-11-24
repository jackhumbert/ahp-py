"""Putting what a session waits on to the user, through MCP elicitation.

An AHP session that needs a person raises an `inputNeeded` entry: permission to
run a tool, a review of a tool's result, or a question. When the MCP client can
show a form (it declared the `elicitation` capability), the entry becomes one:
`prompt_for` turns it into a message and a schema, the client asks the user,
and `forward` sends what the user chose back to the session as the ordinary
AHP answer.

The answer comes from the client's own prompt, never from a tool argument, so
the model can start and follow sessions but cannot approve anything. A client
that cannot show a form gets no prompt; the session keeps waiting for an answer
in one of the user's AHP apps.

What this does not ask: a tool's sign-in (`toolAuthentication`) needs a token,
not a form, and a question that only carries a link has nothing to fill in.
Both are left to an AHP app.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Literal

from mcp.server.mcpserver import Context
from mcp_types import ClientCapabilities
from mcp_types.version import is_version_at_least
from pydantic import BaseModel, ConfigDict, Field, create_model

from ahp_mcp.fleet import Fleet, plain

__all__ = ["Prompt", "can_ask", "forward", "prompt_for"]

#: First MCP revision that carries elicitation inside the tool call's result
#: rather than as a request from server to client over a back channel.
_INPUT_REQUIRED_VERSION: Final = "2026-07-28"
#: Longest tool input shown in a permission prompt.
_INPUT_PREVIEW: Final = 600

_ALLOW: Final = "Allow"
_DENY: Final = "Deny"
_KEEP: Final = "Keep the result"
_DISCARD: Final = "Discard the result"


def can_ask(ctx: Context[Any, Any]) -> bool:
    """Whether this call can put a form to the user.

    The client has to have declared form elicitation (a bare `elicitation: {}`
    predates modes and means forms). Before 2026-07-28 the question also needs
    a channel from server to client; from then on it rides in the result.
    """
    capabilities: ClientCapabilities | None = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    if elicitation is None or (elicitation.form is None and elicitation.url is not None):
        return False
    version = ctx.protocol_version
    if version is not None and is_version_at_least(version, _INPUT_REQUIRED_VERSION):
        return True
    return bool(ctx.session.can_send_request)


@dataclass(frozen=True)
class _Question:
    """One field of a question form, and how to turn its value back into AHP."""

    question_id: str
    kind: str
    #: Shown label to option id, for the select kinds.
    options: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Prompt:
    """One open request as a form: the message, the schema, and the way back."""

    entry_id: str
    kind: Literal["tool", "result", "question"]
    message: str
    schema: type[BaseModel]
    #: For a tool: shown label to `(option id or None, approves)`.
    decisions: Mapping[str, tuple[str | None, bool]] = field(default_factory=dict)
    #: For a question: form field name to the question it answers.
    questions: Mapping[str, _Question] = field(default_factory=dict)


class _Form(BaseModel):
    model_config = ConfigDict(extra="ignore")
    #: Set on each generated form: the prompt it was made for.
    prompt: ClassVar[Prompt | None] = None


def prompt_for(entry: Mapping[str, Any], *, session_title: str = "") -> Prompt | None:
    """The form for one `inputNeeded` entry, or None when it is not a form's to answer."""
    kind = entry.get("kind")
    entry_id = str(entry.get("id") or "")
    if not entry_id:
        return None
    heading = f"A session ({session_title}) " if session_title else "A session "
    if kind == "toolConfirmation":
        call = entry.get("toolCall")
        if not isinstance(call, Mapping):
            return None
        if call.get("status") == "pending-result-confirmation":
            return _result_prompt(entry_id, call, heading)
        return _tool_prompt(entry_id, call, heading)
    if kind == "chatInput":
        request = entry.get("request")
        if isinstance(request, Mapping):
            return _question_prompt(entry_id, request, heading)
    return None


async def forward(fleet: Fleet, session: str, prompt: Prompt, answer: BaseModel) -> str:
    """Send what the user filled in to the session. Returns a line on what was sent."""
    values = answer.model_dump()
    if prompt.kind == "tool":
        choice = str(values.get("decision", ""))
        option_id, approves = prompt.decisions[choice]
        note = str(values.get("note") or "").strip() or None
        sent = await fleet.confirm_tool(
            session,
            prompt.entry_id,
            approved=approves,
            option_id=option_id,
            note=None if approves else note,
        )
        said = f"The user chose {choice!r}."
    elif prompt.kind == "result":
        choice = str(values.get("decision", ""))
        sent = await fleet.confirm_result(session, prompt.entry_id, approved=choice == _KEEP)
        said = f"The user chose {choice!r}."
    else:
        answers = {
            question.question_id: _answer_value(question, values.get(name))
            for name, question in prompt.questions.items()
        }
        sent = await fleet.answer_input(session, prompt.entry_id, answers)
        said = "The user answered the session's question."
    if not sent:
        return "It was answered somewhere else before the user's answer arrived."
    await fleet.settled(session, prompt.entry_id)
    return said


# ─── the three forms ─────────────────────────────────────────────────────


def _tool_prompt(entry_id: str, call: Mapping[str, Any], heading: str) -> Prompt:
    name = plain(call.get("displayName")) or str(call.get("toolName") or "a tool")
    title = plain(call.get("confirmationTitle"))
    lines = [f"{heading}asks to run {name}" + (f" ({title})" if title and title != name else "")]
    action = plain(call.get("invocationMessage")) or plain(call.get("intention"))
    if action:
        lines.append(action)
    shown = _input_preview(call.get("toolInput"))
    if shown:
        lines.append(shown)

    decisions: dict[str, tuple[str | None, bool]] = {}
    for option in call.get("options") or []:
        if not isinstance(option, Mapping) or not option.get("id"):
            continue
        label = _unique(plain(option.get("label")) or str(option["id"]), decisions)
        decisions[label] = (str(option["id"]), option.get("kind") != "deny")
    if not decisions:
        decisions = {_ALLOW: (None, True), _DENY: (None, False)}
    choices = list(decisions)
    schema = _form(
        "PermissionAnswer",
        decision=(
            Literal[tuple(choices)],
            Field(title="Decision", description="Whether the tool may run"),
        ),
        note=(
            str,
            Field("", title="Note", description="Optional, for the agent when you deny"),
        ),
    )
    prompt = Prompt(entry_id, "tool", "\n\n".join(lines), schema, decisions=decisions)
    schema.prompt = prompt
    return prompt


def _result_prompt(entry_id: str, call: Mapping[str, Any], heading: str) -> Prompt:
    name = plain(call.get("displayName")) or str(call.get("toolName") or "a tool")
    lines = [f"{heading}wants you to review what {name} did"]
    done = plain(call.get("pastTenseMessage"))
    if done:
        lines.append(done)
    schema = _form(
        "ResultAnswer",
        decision=(Literal[_KEEP, _DISCARD], Field(title="Decision")),
    )
    prompt = Prompt(entry_id, "result", "\n\n".join(lines), schema)
    schema.prompt = prompt
    return prompt


def _question_prompt(entry_id: str, request: Mapping[str, Any], heading: str) -> Prompt | None:
    questions = [q for q in request.get("questions") or [] if isinstance(q, Mapping)]
    fields: dict[str, Any] = {}
    mapping: dict[str, _Question] = {}
    for index, question in enumerate(questions):
        built = _question_field(question)
        if built is None:
            return None
        name = f"q{index + 1}"
        fields[name], mapping[name] = built
    if not fields:
        # A link-only request has nothing to fill in here.
        return None
    lines = [f"{heading}has a question for you"]
    message = plain(request.get("message"))
    if message:
        lines.append(message)
    schema = _form("QuestionAnswer", **fields)
    prompt = Prompt(entry_id, "question", "\n\n".join(lines), schema, questions=mapping)
    schema.prompt = prompt
    return prompt


def _question_field(question: Mapping[str, Any]) -> tuple[Any, _Question] | None:
    question_id = str(question.get("id") or "")
    kind = str(question.get("kind") or "")
    if not question_id:
        return None
    title = plain(question.get("title")) or plain(question.get("message")) or question_id
    description = plain(question.get("message")) if question.get("title") else None
    required = bool(question.get("required"))
    extra: dict[str, Any] = {"title": title}
    if description:
        extra["description"] = description

    options: dict[str, str] = {}
    annotation: Any
    if kind == "text":
        annotation = str
        if isinstance(question.get("min"), int | float):
            extra["min_length"] = int(question["min"])
        if isinstance(question.get("max"), int | float):
            extra["max_length"] = int(question["max"])
        default = question.get("defaultValue")
    elif kind in ("number", "integer"):
        annotation = int if kind == "integer" else float
        if isinstance(question.get("min"), int | float):
            extra["ge"] = question["min"]
        if isinstance(question.get("max"), int | float):
            extra["le"] = question["max"]
        default = question.get("defaultValue")
    elif kind == "boolean":
        annotation = bool
        default = question.get("defaultValue")
    elif kind in ("single-select", "multi-select"):
        for option in question.get("options") or []:
            if isinstance(option, Mapping) and option.get("id"):
                label = _unique(plain(option.get("label")) or str(option["id"]), options)
                options[label] = str(option["id"])
        if not options:
            return None
        choice: Any = Literal[tuple(options)]
        annotation = list[choice] if kind == "multi-select" else choice
        default = None
    else:
        return None

    if required:
        spec = Field(**extra)
    elif default is not None:
        spec = Field(default, **extra)
    else:
        annotation = annotation | None
        spec = Field(None, **extra)
    return (annotation, spec), _Question(question_id, kind, options)


def _answer_value(question: _Question, value: Any) -> Any:
    """A form value as the AHP answer: option labels back to option ids."""
    if value is None:
        return None
    if question.kind == "single-select":
        return question.options.get(str(value), str(value))
    if question.kind == "multi-select":
        items: Sequence[Any] = value if isinstance(value, list) else [value]
        return [question.options.get(str(item), str(item)) for item in items]
    return value


# ─── helpers ─────────────────────────────────────────────────────────────


def _form(name: str, **fields: Any) -> type[_Form]:
    form: type[_Form] = create_model(name, __base__=_Form, **fields)
    return form


def _unique(label: str, taken: Mapping[str, Any]) -> str:
    candidate, number = label, 2
    while candidate in taken:
        candidate = f"{label} ({number})"
        number += 1
    return candidate


def _input_preview(tool_input: Any) -> str:
    """The tool's input, short enough to read in a prompt."""
    if isinstance(tool_input, Mapping):
        if "uri" in tool_input and len(tool_input) <= 3:
            return ""
        text = json.dumps(tool_input, indent=2, ensure_ascii=False)
    elif isinstance(tool_input, str):
        text = tool_input
        with contextlib.suppress(ValueError):
            text = json.dumps(json.loads(tool_input), indent=2, ensure_ascii=False)
    else:
        return ""
    text = text.strip()
    if len(text) > _INPUT_PREVIEW:
        text = text[:_INPUT_PREVIEW].rstrip() + "\n..."
    return text
