"""`AskUserQuestion`, put to the user as an input request and answered back."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, InputOutcome, UserMessage
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk import UserMessage as SdkUserMessage

from ahp_host_claude import questions
from ahp_host_claude.permissions import QUESTION_TOOL, pre_tool_use_decision
from ahp_host_claude.provider import ClaudeProvider
from tests.fakes import FakeClient, RecordingSink, Step, eventually

ASKED: dict[str, Any] = {
    "questions": [
        {
            "question": "Which library should we use?",
            "header": "Library",
            "options": [
                {"label": "date-fns", "description": "Small and modular"},
                {"label": "dayjs", "description": "Moment-like"},
            ],
            "multiSelect": False,
        },
        {
            "question": "Which features?",
            "header": "Features",
            "options": [{"label": "Parsing"}, {"label": "Formatting"}, {"label": "Zones"}],
            "multiSelect": True,
        },
    ]
}


def _selected(value: str, *freeform: str) -> dict[str, Any]:
    answer: dict[str, Any] = {"kind": "selected", "value": value}
    if freeform:
        answer["freeformValues"] = list(freeform)
    return {"state": "submitted", "value": answer}


class TestTheRequest:
    def test_each_question_becomes_a_select_question(self) -> None:
        request = questions.input_request(ASKED)
        assert request is not None
        first, second = request.questions
        assert (first.id, first.kind, first.message) == (
            "q0",
            "single-select",
            "Which library should we use?",
        )
        assert first.options == [
            {"id": "date-fns", "label": "date-fns", "description": "Small and modular"},
            {"id": "dayjs", "label": "dayjs", "description": "Moment-like"},
        ]
        # Claude Code adds "Other" itself: the user may always type an answer.
        assert first.extra == {"title": "Library", "allowFreeformInput": True}
        assert second.kind == "multi-select"

    def test_text_and_number_questions_keep_their_kind(self) -> None:
        request = questions.input_request(
            {
                "title": "Before I start",
                "questions": [
                    {"question": "Name it?", "header": "Name", "kind": "text"},
                    {
                        "question": "How many?",
                        "header": "Count",
                        "kind": "number",
                        "min": 1,
                        "max": 10,
                        "defaultValue": 3,
                    },
                ],
            }
        )
        assert request is not None
        assert request.message == "Before I start"
        text, number = request.questions
        assert (text.kind, text.options) == ("text", ())
        assert number.kind == "number"
        assert number.extra == {"title": "Count", "min": 1, "max": 10, "defaultValue": 3}

    def test_nothing_askable_is_no_request(self) -> None:
        assert questions.input_request({}) is None
        assert questions.input_request({"questions": [{"question": "x", "options": []}]}) is None


class TestTheAnswers:
    def test_answers_are_keyed_by_question_text_and_joined(self) -> None:
        outcome = InputOutcome(
            response="accept",
            answers={
                "q0": _selected("dayjs"),
                "q1": {
                    "state": "submitted",
                    "value": {"kind": "selected-many", "value": ["Parsing", "Zones"]},
                },
            },
        )
        result = questions.permission_result(ASKED, outcome)
        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input is not None
        assert result.updated_input["answers"] == {
            "Which library should we use?": "dayjs",
            "Which features?": "Parsing, Zones",
        }
        # The rest of the input is what Claude asked, untouched.
        assert result.updated_input["questions"] == ASKED["questions"]

    def test_a_typed_answer_stands_in_for_an_option(self) -> None:
        outcome = InputOutcome(response="accept", answers={"q0": _selected("", "luxon")})
        assert questions.answers_of(ASKED, outcome) == {"Which library should we use?": "luxon"}

    def test_numbers_are_written_as_claude_code_checks_them(self) -> None:
        asked = {"questions": [{"question": "How many?", "kind": "number", "min": 0, "max": 9}]}
        whole = {"q0": {"state": "submitted", "value": {"kind": "number", "value": 3.0}}}
        part = {"q0": {"state": "submitted", "value": {"kind": "number", "value": 2.5}}}
        assert questions.answers_of(asked, InputOutcome("accept", whole)) == {"How many?": "3"}
        assert questions.answers_of(asked, InputOutcome("accept", part)) == {"How many?": "2.5"}

    def test_a_skipped_question_is_left_unanswered(self) -> None:
        outcome = InputOutcome(
            response="accept", answers={"q0": {"state": "skipped"}, "q1": _selected("Parsing")}
        )
        assert questions.answers_of(ASKED, outcome) == {"Which features?": "Parsing"}

    def test_declining_or_dismissing_denies_the_tool_and_says_which(self) -> None:
        declined = questions.permission_result(ASKED, InputOutcome(response="decline"))
        dismissed = questions.permission_result(ASKED, InputOutcome(response="cancel"))
        assert isinstance(declined, PermissionResultDeny)
        assert isinstance(dismissed, PermissionResultDeny)
        assert declined.message == questions.DECLINED
        assert dismissed.message == questions.DISMISSED
        assert not declined.interrupt  # the turn carries on without the answers


def test_the_hook_always_sends_questions_to_the_approval_callback() -> None:
    """Security-neutral: it asks more, never less. An `allow` rule in the
    user's settings must not run the tool with nobody asked."""
    for mode in ("default", "acceptEdits", "auto", "plan"):
        decision = pre_tool_use_decision(QUESTION_TOOL, mode)
        assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )


def _asking(results: list[Any]) -> Step:
    async def step(options: ClaudeAgentOptions) -> None:
        assert options.can_use_tool is not None
        context = ToolPermissionContext(tool_use_id="q1")
        results.append(await options.can_use_tool(QUESTION_TOOL, dict(ASKED), context))

    return step


def _session_harness(tmp_path: Path, steps: list[Step]) -> tuple[ClaudeProvider, list[FakeClient]]:
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        clients.append(FakeClient(options, [steps]))
        return clients[-1]

    return ClaudeProvider(tmp_path, client_factory=factory), clients


def _context() -> AgentSessionContext:
    return AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude")


async def test_a_question_is_asked_in_the_turn_and_answered_back(tmp_path: Path) -> None:
    results: list[Any] = []
    provider, clients = _session_harness(
        tmp_path,
        [
            AssistantMessage(content=[ToolUseBlock("q1", QUESTION_TOOL, ASKED)], model="m"),
            _asking(results),
            SdkUserMessage(content=[ToolResultBlock("q1", "User has answered", False)]),
            _result(),
        ],
    )
    session = await provider.create_session(_context())
    sink = RecordingSink()
    sink.input_outcome = InputOutcome(response="accept", answers={"q0": _selected("date-fns")})
    await session.send_user_message(UserMessage(text="pick one"), sink)

    assert clients[0].options.disallowed_tools == []  # offered to Claude now
    (request,) = sink.inputs
    assert [q.id for q in request.questions] == ["q0", "q1"]
    (result,) = results
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["answers"] == {"Which library should we use?": "date-fns"}
    # A row for the call, but no approval prompt: the questions are the prompt.
    assert ("started", "q1", QUESTION_TOOL, "Ask you") in sink.events
    assert sink.confirmations == []
    assert sink.past_tense["q1"] == "Asked: Which library should we use?"


async def test_answered_on_another_device_withdraws_nothing_it_should_not(
    tmp_path: Path,
) -> None:
    """Remote Control: claude.ai answered, and the CLI cancelled the ask here."""
    asks: list[asyncio.Task[Any]] = []

    async def asking(options: ClaudeAgentOptions) -> None:
        assert options.can_use_tool is not None
        can_use_tool = options.can_use_tool
        context = ToolPermissionContext(tool_use_id="q1")

        async def ask() -> Any:
            return await can_use_tool(QUESTION_TOOL, dict(ASKED), context)

        asks.append(asyncio.create_task(ask()))
        await eventually(lambda: bool(sink.inputs))
        asks[0].cancel()

    provider, _ = _session_harness(
        tmp_path,
        [
            AssistantMessage(content=[ToolUseBlock("q1", QUESTION_TOOL, ASKED)], model="m"),
            asking,
            SdkUserMessage(content=[ToolResultBlock("q1", "User has answered", False)]),
            _result(),
        ],
    )
    session = await provider.create_session(_context())
    sink = RecordingSink()
    sink.input_outcome = None  # nobody here answers
    await session.send_user_message(UserMessage(text="pick one"), sink)
    assert asks[0].cancelled()
    # Not reported as an approval given elsewhere: there was no approval prompt.
    assert not any(e[0] == "confirmed_elsewhere" for e in sink.events)
    assert any(e[0] == "completed" and e[1] == "q1" for e in sink.events)
