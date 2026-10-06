"""Claude Code's `AskUserQuestion`, put to the user as an AHP input request.

The tool's input is one to four questions, each with a short ``header``, a
``question`` and two to four ``options`` (``label``, ``description``), and a
``multiSelect`` flag; newer Claude Code adds a ``kind`` of ``choice``, ``text``
or ``number``, with ``min``/``max``/``defaultValue`` for numbers, and a
``title`` over the whole form. Claude Code asks permission for it like any
tool (its own `checkPermissions` answers ``ask``), and an SDK host answers by
allowing it with ``updatedInput.answers`` filled in: question text -> answer
string, several choices joined by ``", "``. Read from the CLI bundled with the
SDK (the tool's input and output schemas, and its `call`).

So the approval callback is where this lives. The questions become one
`InputRequest` (`chat/inputRequested`): a choice question is ``single-select``
or ``multi-select`` with free-form input allowed, because Claude Code always
offers "Other" itself and tells the model not to list one; a text question is
``text``; a number is ``number``. Option ids are the labels, which Claude Code
already requires to be unique within a question and is the answer it wants
back.

Declining the request denies the tool, with a message saying so, and the model
carries on without the answers; dismissing it does the same. Neither stops the
turn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from ahp_host.provider.base import InputOutcome, InputQuestion, InputRequest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from ahp_host_claude.permissions import QUESTION_TOOL

__all__ = ["QUESTION_TOOL", "answers_of", "input_request", "permission_result"]

#: What the model reads when the user turns the questions down.
DECLINED: Final = "The user declined to answer these questions."
DISMISSED: Final = "The user dismissed these questions without answering."
UNREADABLE: Final = "These questions could not be shown to the user."


def _questions(tool_input: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = tool_input.get("questions")
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return []
    return [q for q in raw if isinstance(q, Mapping) and isinstance(q.get("question"), str)]


def _qid(index: int) -> str:
    return f"q{index}"


def _options(question: Mapping[str, Any]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    raw = question.get("options")
    for option in raw if isinstance(raw, Sequence) and not isinstance(raw, str) else ():
        if not isinstance(option, Mapping):
            continue
        label = option.get("label")
        if not isinstance(label, str) or not label:
            continue
        entry: dict[str, Any] = {"id": label, "label": label}
        description = option.get("description")
        if isinstance(description, str) and description:
            entry["description"] = description
        options.append(entry)
    return options


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _question(index: int, question: Mapping[str, Any]) -> InputQuestion | None:
    text = str(question["question"])
    description = question.get("description")
    message = f"{text}\n\n{description}" if isinstance(description, str) and description else text
    extra: dict[str, Any] = {}
    header = question.get("header")
    if isinstance(header, str) and header:
        extra["title"] = header
    kind = question.get("kind", "choice")
    if kind == "text":
        return InputQuestion(id=_qid(index), kind="text", message=message, extra=extra)
    if kind == "number":
        for key in ("min", "max", "defaultValue"):
            if _number(question.get(key)):
                extra[key] = question[key]
        return InputQuestion(id=_qid(index), kind="number", message=message, extra=extra)
    options = _options(question)
    if not options:
        return None
    multi = question.get("multiSelect") is True
    return InputQuestion(
        id=_qid(index),
        kind="multi-select" if multi else "single-select",
        message=message,
        options=options,
        # Claude Code adds "Other" itself and tells the model never to list one.
        extra={**extra, "allowFreeformInput": True},
    )


def input_request(tool_input: Mapping[str, Any]) -> InputRequest | None:
    """The questions as one input request, or None if there is nothing to ask."""
    questions: list[InputQuestion] = []
    for index, question in enumerate(_questions(tool_input)):
        asked = _question(index, question)
        if asked is not None:
            questions.append(asked)
    if not questions:
        return None
    title = tool_input.get("title")
    return InputRequest(
        message=title if isinstance(title, str) and title else None, questions=questions
    )


def _format_number(value: float) -> str:
    """As Claude Code checks a number answer: digits, an optional fraction, no exponent."""
    if float(value).is_integer():
        return str(int(value))
    return format(value, "f").rstrip("0").rstrip(".")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _answer_text(answer: Any) -> str | None:
    """One `ChatInputAnswer` as the string Claude Code wants, or None if unanswered."""
    if not isinstance(answer, Mapping):
        return None
    if answer.get("state") == "skipped":
        typed = _strings(answer.get("freeformValues"))
        return ", ".join(typed) or None
    value = answer.get("value")
    if not isinstance(value, Mapping):
        return None
    kind, raw = value.get("kind"), value.get("value")
    parts: list[str]
    if kind in ("selected", "selected-many", "text"):
        parts = _strings(raw)
    elif kind == "number" and isinstance(raw, int | float) and not isinstance(raw, bool):
        parts = [_format_number(raw)]
    elif kind == "boolean" and isinstance(raw, bool):
        parts = ["Yes" if raw else "No"]
    else:
        parts = []
    parts += _strings(value.get("freeformValues"))
    return ", ".join(parts) or None


def answers_of(tool_input: Mapping[str, Any], outcome: InputOutcome) -> dict[str, str]:
    """Question text -> answer, for the questions the user answered."""
    answers: dict[str, str] = {}
    for index, question in enumerate(_questions(tool_input)):
        text = _answer_text(outcome.answers.get(_qid(index)))
        if text is not None:
            answers[str(question["question"])] = text
    return answers


def permission_result(
    tool_input: Mapping[str, Any], outcome: InputOutcome
) -> PermissionResultAllow | PermissionResultDeny:
    """What the approval callback answers once the user has.

    An accepted form allows the tool with the answers in its input, which is
    how Claude Code's own permission dialog hands them over. One the user
    answered nothing on is still allowed: Claude Code tells the model "the user
    did not answer the questions", which is true.
    """
    if outcome.response == "decline":
        return PermissionResultDeny(message=DECLINED)
    if not outcome.accepted:
        return PermissionResultDeny(message=DISMISSED)
    return PermissionResultAllow(
        updated_input={**tool_input, "answers": answers_of(tool_input, outcome)}
    )
