"""The elicitation vocabulary: questions, answers, and the two actions.

``chat/inputRequested`` opens a request and ``chat/inputCompleted`` closes it.
Between them a client MAY sync one question's draft with
``chat/inputAnswerChanged`` -- a *different* action, requiring
``[type, requestId, questionId]`` and carrying a **singular** ``answer``, with no
response and no ``answers`` map at all. Sending it in place of the completion
leaves the request unresolved: the host stays parked on the future it opened, the
chat stays at ``InputNeeded``, and the turn never ends.

The two vocabularies do not line up by name, which is the other half of why this
is here rather than in a dict literal at each call site. A ``single-select``
answers with ``selected`` even though what the caller holds is a string, an
``integer`` question answers with ``number``, and ``multi-select`` answers with
``selected-many``. Encoding from the question the host sent is the only way to
get those right without reading the schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from agent_host_protocol.types import JsonObject

__all__ = [
    "AnswerState",
    "ResponseKind",
    "encode_answer",
    "encode_answers",
    "input_answer_changed",
    "input_completed",
]

#: ``ChatInputResponseKind`` -- how a client completed an input request.
ResponseKind = Literal["accept", "decline", "cancel"]
#: ``ChatInputAnswered.state``. The third state, ``skipped``, is a *different*
#: shape (it carries no value at all), which is why it is not in this alias.
AnswerState = Literal["draft", "submitted"]

#: ``ChatInputQuestion.kind`` -> the ``ChatInputAnswerValue.kind`` answering it.
_VALUE_KINDS: Mapping[str, str] = {
    "text": "text",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
    "single-select": "selected",
    "multi-select": "selected-many",
}


def input_completed(
    request_id: str,
    response: ResponseKind,
    answers: Mapping[str, Any] | None = None,
) -> JsonObject:
    """``chat/inputCompleted``; required ``[type, requestId, response]``.

    *answers* is optional and is an **overlay**, not a replacement: the reducer
    merges it over the drafts already on the part, and the host then reads the
    merged result back out of state rather than off this action. So a client
    answering one question of three does not have to carry the other two.
    """
    action: JsonObject = {
        "type": "chat/inputCompleted",
        "requestId": _request_id(request_id),
        "response": response,
    }
    if answers:
        action["answers"] = dict(answers)
    return action


def input_answer_changed(
    request_id: str, question_id: str, answer: Mapping[str, Any] | None = None
) -> JsonObject:
    """``chat/inputAnswerChanged``; required ``[type, requestId, questionId]``.

    Draft sync for **one** question, so a user answering on client A is visible
    on client B. It never resolves the request -- only `input_completed` does.

    Omitting *answer* clears that question's draft. The reducer keys the
    deletion on the key being ABSENT (`"answer" not in action`), and treats an
    explicit ``null`` as a stored value, so this must not serialise one.
    """
    action: JsonObject = {
        "type": "chat/inputAnswerChanged",
        "requestId": _request_id(request_id),
        "questionId": question_id,
    }
    if answer is not None:
        action["answer"] = dict(answer)
    return action


def encode_answers(
    values: Mapping[str, Any], questions: Sequence[Any] = ()
) -> dict[str, JsonObject]:
    """One ``ChatInputAnswer`` per entry of *values*, keyed by question id."""
    by_id = {str(q.get("id", "")): q for q in questions if isinstance(q, Mapping)}
    return {str(key): encode_answer(value, by_id.get(str(key))) for key, value in values.items()}


def encode_answer(
    value: Any,
    question: Mapping[str, Any] | None = None,
    *,
    state: AnswerState = "submitted",
) -> JsonObject:
    """A ``ChatInputAnswer`` for *value*, shaped by *question*'s kind.

    A value that already carries a ``state`` passes through untouched: a caller
    who has read the schema and assembled ``freeformValues``, or one deliberately
    sending ``{"state": "skipped"}``, must not have it rewritten. A bare ``None``
    is also ``skipped`` -- a real answer state ("free-form reason or value
    captured while skipping") rather than the absence of one.
    """
    if isinstance(value, Mapping) and "state" in value:
        return dict(value)
    if value is None:
        return {"state": "skipped"}
    return {"state": state, "value": _answer_value(value, question)}


def _answer_value(value: Any, question: Mapping[str, Any] | None) -> JsonObject:
    kind = None
    if isinstance(question, Mapping):
        kind = _VALUE_KINDS.get(str(question.get("kind", "")))
    if kind is None:
        kind = _inferred_kind(value)
    if kind == "selected-many":
        items = value if isinstance(value, list | tuple) else [value]
        return {"kind": kind, "value": [str(item) for item in items]}
    if kind == "boolean":
        return {"kind": kind, "value": bool(value)}
    if kind == "number":
        return {"kind": kind, "value": value}
    # `text` and `selected` are both a plain string on the wire; which one it is
    # is decided by the question, never by the value.
    return {"kind": kind, "value": str(value)}


def _inferred_kind(value: Any) -> str:
    """The value kind for an answer whose question we do not have.

    ``ChatInputRequest.questions`` is optional -- a URL-style elicitation
    carries a `url` and nothing to ask -- so an entry can legitimately be
    answered with no question to read the kind from. `bool` is tested first
    because it is a subclass of `int` and would otherwise answer as a number.
    """
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list | tuple):
        return "selected-many"
    return "text"


def _request_id(value: str) -> str:
    """Reject an empty id rather than putting one on the wire.

    The id lives at ``request.id``; ``chat/inputRequested`` has no ``requestId``
    property at all, so a client reading one sends ``""`` and the host answers
    "no open input request" -- a rejection that looks like a race with another
    client rather than a bug on this side.
    """
    if not value:
        raise ValueError("requestId is required on this action and was empty")
    return value
