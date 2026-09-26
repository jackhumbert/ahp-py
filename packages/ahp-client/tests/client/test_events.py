"""The action -> event mapping, held exhaustive against the shared action table.

Five defects shipped here as one shape. A key spelled from memory rather than
from the schema -- ``chat/turnFailed``, ``chat/reasoningDelta``,
``chat/titleChanged`` and ``chat/toolCallResultReview`` are none of them real
actions -- and an action with no key at all fail *identically*: the caller gets
an ``UnknownEvent``, or, for a terminal action, silence until ``idle_timeout``.
No behavioural test notices a class that is merely unreachable, which is why
:func:`test_the_mapping_covers_every_chat_action` exists and is worth more than
the individual fixes: it fails the moment the table and the spec disagree, in
either direction.
"""

from __future__ import annotations

import pytest
from agent_host_protocol import ACTION_TYPES

from agent_host_client import (
    InputRequested,
    Reasoning,
    ToolCallCompleted,
    ToolCallContentChanged,
    ToolCallDelta,
    ToolCallReady,
    ToolCallResultReview,
    ToolCallRunning,
    ToolInfo,
    TurnFailed,
    event_for,
)
from agent_host_client.api.events import _BY_TYPE, _NOT_MODELLED, is_modelled

CHAT = "ahp-chat:/c"

CHAT_ACTIONS = frozenset(t for t in ACTION_TYPES if t.startswith("chat/"))


def _envelope(action: dict[str, object]) -> dict[str, object]:
    return {"channel": CHAT, "action": action, "serverSeq": 1}


class _Sent:
    """Collects what an event dispatches, so the wire shape can be asserted."""

    def __init__(self) -> None:
        self.actions: list[dict[str, object]] = []

    def __call__(self, channel: str, action: object) -> None:
        assert channel == CHAT
        assert isinstance(action, dict)
        self.actions.append(dict(action))


# -- the contract -------------------------------------------------------------


def test_the_mapping_covers_every_chat_action() -> None:
    """Every chat action a host may send is modelled or deliberately skipped."""
    mapped = frozenset(t for t in _BY_TYPE if t.startswith("chat/"))
    assert mapped | _NOT_MODELLED == CHAT_ACTIONS
    # Disjoint, or a type could be "skipped" while also mapped and the equality
    # above would still hold with a hole elsewhere.
    assert not (mapped & _NOT_MODELLED)


def test_no_key_is_invented() -> None:
    """A key the spec does not define can never fire, and nothing says so."""
    assert set(_BY_TYPE) <= set(ACTION_TYPES)
    assert _NOT_MODELLED <= CHAT_ACTIONS


# -- the individual actions ---------------------------------------------------


def test_chat_error_is_the_terminal_failure() -> None:
    """`chat/error` is the schema's only failure action; there is no
    `chat/turnFailed`."""
    event = event_for(
        _envelope(
            {
                "type": "chat/error",
                "turnId": "t1",
                "duration": 12,
                "error": {"errorType": "agent.turn", "message": "RuntimeError: kaboom"},
            }
        )
    )
    assert isinstance(event, TurnFailed)
    assert event.reason == "RuntimeError: kaboom"
    assert event.error_type == "agent.turn"


def test_a_0_9_chat_error_carries_its_error_in_the_part() -> None:
    """0.9.0 moved `ErrorInfo` into an `ErrorResponsePart`. The legacy shape
    above still arrives from a host that negotiated an earlier version, so both
    must read the same."""
    event = event_for(
        _envelope(
            {
                "type": "chat/error",
                "turnId": "t1",
                "duration": 12,
                "part": {
                    "kind": "error",
                    "error": {"errorType": "agent.turn", "message": "RuntimeError: kaboom"},
                },
            }
        )
    )
    assert isinstance(event, TurnFailed)
    assert event.reason == "RuntimeError: kaboom"
    assert event.error_type == "agent.turn"


def test_reasoning_carries_its_part_id() -> None:
    """The action is `chat/reasoning`, and it targets a part the host created
    with an earlier `chat/responsePart`."""
    event = event_for(
        _envelope(
            {"type": "chat/reasoning", "turnId": "t1", "partId": "re-1", "content": "thinking"}
        )
    )
    assert isinstance(event, Reasoning)
    assert event.text == "thinking"
    assert event.part_id == "re-1"


def test_tool_call_progress_is_modelled() -> None:
    """The two actions a slow tool reports progress with."""
    delta = event_for(
        _envelope(
            {
                "type": "chat/toolCallDelta",
                "turnId": "t1",
                "toolCallId": "tc1",
                "content": '{"path": "a',
                "invocationMessage": "Reading a.py",
            }
        )
    )
    assert isinstance(delta, ToolCallDelta)
    assert delta.content == '{"path": "a'
    assert delta.invocation_message == "Reading a.py"

    changed = event_for(
        _envelope(
            {
                "type": "chat/toolCallContentChanged",
                "turnId": "t1",
                "toolCallId": "tc1",
                "content": [{"type": "text", "text": "alpha"}],
            }
        )
    )
    assert isinstance(changed, ToolCallContentChanged)
    assert changed.tool_call_id == "tc1"
    assert [dict(c) for c in changed.content] == [{"type": "text", "text": "alpha"}]


def test_a_markdown_invocation_message_is_flattened() -> None:
    """`StringOrMarkdown` is a plain string *or* `{markdown: str}`; a consumer
    rendering `.invocation_message` must not have to branch on which."""
    delta = event_for(
        _envelope(
            {
                "type": "chat/toolCallDelta",
                "turnId": "t1",
                "toolCallId": "tc1",
                "invocationMessage": {"markdown": "Reading `a.py`"},
            }
        )
    )
    assert isinstance(delta, ToolCallDelta)
    assert delta.invocation_message == "Reading `a.py`"


def test_an_auto_confirmed_ready_is_a_transition_not_a_question() -> None:
    """`chat/toolCallReady` carrying `confirmed` "transitions directly to
    `running`". A host emits one for every call it does not gate, so treating it
    as an approval request means answering on the common path -- and the host
    rejects that answer, because the call was never pending."""
    pending = event_for(
        _envelope({"type": "chat/toolCallReady", "turnId": "t1", "toolCallId": "tc1"})
    )
    assert isinstance(pending, ToolCallReady)

    running = event_for(
        _envelope(
            {
                "type": "chat/toolCallReady",
                "turnId": "t1",
                "toolCallId": "tc1",
                "confirmed": "not-needed",
                "contributor": {"kind": "client", "clientId": "me"},
            }
        )
    )
    assert isinstance(running, ToolCallRunning)
    assert running.confirmed == "not-needed"
    assert running.contributor == {"kind": "client", "clientId": "me"}


def test_a_result_review_is_the_completion_itself() -> None:
    """There is no `chat/toolCallResultReview` action in any version of the
    schema; a review is a `chat/toolCallComplete` carrying
    `requiresResultConfirmation`. Keyed on the invented name, the review class
    could never be constructed and the turn hung on an answer nobody sent."""
    result = {"success": True, "pastTenseMessage": "Read the file"}
    plain = event_for(
        _envelope(
            {
                "type": "chat/toolCallComplete",
                "turnId": "t1",
                "toolCallId": "tc1",
                "result": result,
            }
        )
    )
    assert isinstance(plain, ToolCallCompleted)

    sent = _Sent()
    review = event_for(
        _envelope(
            {
                "type": "chat/toolCallComplete",
                "turnId": "t1",
                "toolCallId": "tc1",
                "result": result,
                "requiresResultConfirmation": True,
            }
        ),
        sent,
    )
    assert isinstance(review, ToolCallResultReview)
    assert review.result == result
    review.confirm()
    assert sent.actions == [
        {
            "type": "chat/toolCallResultConfirmed",
            "turnId": "t1",
            "toolCallId": "tc1",
            "approved": True,
        }
    ]


def test_a_tool_call_is_named_from_state_because_no_action_names_it() -> None:
    """`toolName` is published once, on `chat/toolCallStart`; `annotations` is a
    property of `ToolDefinition` and appears on no action at all. Reading either
    off a `chat/toolCallReady` yields nothing, and an `ApprovalPolicy` switching
    on them denies everything, silently."""
    envelope = _envelope({"type": "chat/toolCallReady", "turnId": "t1", "toolCallId": "tc1"})
    assert "toolName" not in str(envelope)
    assert "annotations" not in str(envelope)

    blind = event_for(envelope)
    assert isinstance(blind, ToolCallReady)
    assert blind.tool_name == ""

    def lookup(channel: str, tool_call_id: str) -> ToolInfo:
        assert (channel, tool_call_id) == (CHAT, "tc1")
        return ToolInfo("read_file", {"readOnlyHint": True})

    informed = event_for(envelope, None, lookup)
    assert isinstance(informed, ToolCallReady)
    assert informed.tool_name == "read_file"
    assert informed.annotations == {"readOnlyHint": True}


# -- elicitation --------------------------------------------------------------


def test_an_elicitation_is_read_from_request_not_from_a_request_id() -> None:
    """`ChatInputRequestedAction` is `{type, request}` -- it has no `requestId`
    property, and everything a consumer needs to render the prompt is nested
    under `request`."""
    event = event_for(
        _envelope(
            {
                "type": "chat/inputRequested",
                "turnId": "t1",
                "request": {
                    "id": "in-1",
                    "message": "Echo it back how?",
                    "questions": [
                        {
                            "id": "style",
                            "kind": "single-select",
                            "message": "Style?",
                            "options": [{"id": "shout", "label": "SHOUT"}],
                        }
                    ],
                    "answers": {"style": {"state": "draft", "value": {"kind": "selected"}}},
                },
            }
        )
    )
    assert isinstance(event, InputRequested)
    assert event.request_id == "in-1"
    assert event.message == "Echo it back how?"
    assert [q["id"] for q in event.questions] == ["style"]
    assert set(event.answers) == {"style"}


def test_answering_completes_the_request_rather_than_drafting_at_it() -> None:
    """`chat/inputAnswerChanged` is draft sync for ONE question and never
    resolves anything; the host stays parked on its own future. Only
    `chat/inputCompleted` ends the request -- and its answer values are shaped by
    the question's kind, so a single-select answers `selected`, not `text`."""
    sent = _Sent()
    event = event_for(
        _envelope(
            {
                "type": "chat/inputRequested",
                "turnId": "t1",
                "request": {
                    "id": "in-1",
                    "questions": [
                        {"id": "style", "kind": "single-select", "message": "?", "options": []},
                        {"id": "loud", "kind": "boolean", "message": "?"},
                        {"id": "times", "kind": "integer", "message": "?"},
                        {"id": "tags", "kind": "multi-select", "message": "?", "options": []},
                        {"id": "note", "kind": "text", "message": "?"},
                    ],
                },
            }
        ),
        sent,
    )
    assert isinstance(event, InputRequested)
    event.answer({"style": "shout", "loud": True, "times": 3, "tags": ["a", "b"], "note": "hi"})
    assert sent.actions == [
        {
            "type": "chat/inputCompleted",
            "requestId": "in-1",
            "response": "accept",
            "answers": {
                "style": {"state": "submitted", "value": {"kind": "selected", "value": "shout"}},
                "loud": {"state": "submitted", "value": {"kind": "boolean", "value": True}},
                "times": {"state": "submitted", "value": {"kind": "number", "value": 3}},
                "tags": {
                    "state": "submitted",
                    "value": {"kind": "selected-many", "value": ["a", "b"]},
                },
                "note": {"state": "submitted", "value": {"kind": "text", "value": "hi"}},
            },
        }
    ]


def test_declining_and_cancelling_carry_the_response_kind() -> None:
    """`decline` and `cancel` are outcomes of the same action, not other
    actions -- and `answers` is omitted rather than sent empty."""
    sent = _Sent()
    envelope = _envelope({"type": "chat/inputRequested", "turnId": "t1", "request": {"id": "in-1"}})
    decline = event_for(envelope, sent)
    cancel = event_for(envelope, sent)
    assert isinstance(decline, InputRequested)
    assert isinstance(cancel, InputRequested)
    decline.decline()
    cancel.cancel()
    assert sent.actions == [
        {"type": "chat/inputCompleted", "requestId": "in-1", "response": "decline"},
        {"type": "chat/inputCompleted", "requestId": "in-1", "response": "cancel"},
    ]


def test_an_answer_already_shaped_is_passed_through() -> None:
    """A caller that read the schema and assembled `freeformValues` -- which the
    one-value-per-question encoding has no way to express -- must not have it
    rewritten."""
    sent = _Sent()
    event = event_for(
        _envelope(
            {
                "type": "chat/inputRequested",
                "turnId": "t1",
                "request": {
                    "id": "in-1",
                    "questions": [{"id": "tags", "kind": "multi-select", "message": "?"}],
                },
            }
        ),
        sent,
    )
    assert isinstance(event, InputRequested)
    answer = {
        "state": "submitted",
        "value": {"kind": "selected-many", "value": ["a"], "freeformValues": ["other"]},
    }
    event.answer({"tags": answer})
    assert sent.actions[0]["answers"] == {"tags": answer}


def test_an_empty_request_id_is_refused_instead_of_being_sent() -> None:
    """The id lives at `request.id`; reading a `requestId` yields "" and the
    host answers "no open input request", which reads like a race with another
    client rather than a bug on this side."""
    sent = _Sent()
    event = event_for(_envelope({"type": "chat/inputRequested", "turnId": "t1"}), sent)
    assert isinstance(event, InputRequested)
    with pytest.raises(ValueError, match="requestId"):
        event.answer({})
    assert sent.actions == []


def test_a_failure_with_no_error_info_still_reads_as_a_string() -> None:
    """`ErrorInfo` is required on `chat/error`, but a `TurnFailed` is also
    minted by `TurnStream` for a rejection and for a dead stream."""
    event = event_for(_envelope({"type": "chat/error", "turnId": "t1", "duration": 1}))
    assert isinstance(event, TurnFailed)
    assert event.reason == ""
    assert event.error_type == ""


class TestDeliberatelyUnmodelled:
    """`UnknownEvent` is forward compatibility; `_NOT_MODELLED` is a decision.

    Delivering both the same way conflated them, and the conflation had a cost:
    a caller watching for `UnknownEvent` to detect a version mismatch got one on
    every turn it approved a tool or answered a question, because its own write
    echoed back through the stream.
    """

    def test_an_echo_of_our_own_write_is_not_delivered(self) -> None:
        for action_type in ("chat/toolCallConfirmed", "chat/inputCompleted"):
            assert not is_modelled({"action": {"type": action_type}}), action_type

    def test_a_real_turn_event_is(self) -> None:
        assert is_modelled({"action": {"type": "chat/delta"}})

    def test_an_action_from_a_newer_host_still_is(self) -> None:
        """The whole point of keeping `UnknownEvent`: a build that has never
        heard of an action must still hand it to the caller rather than
        silently swallowing it."""
        assert is_modelled({"action": {"type": "chat/somethingFromTheFuture"}})

    def test_a_malformed_envelope_does_not_raise(self) -> None:
        assert is_modelled({})
        assert is_modelled({"action": "not a mapping"})
