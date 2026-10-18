"""The gate forms, their answers and `MainChatGates` against a recording sink (plan §6)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from ahp_host.provider.base import (
    InputOutcome,
    InputQuestion,
    InputRequest,
    SessionPublisher,
    ToolConfirmation,
    ToolConfirmationOutcome,
    TurnSink,
)
from ahp_host.provider.changes import FileChange

from ahp_host_flow.engine import GateCall, Redactor
from ahp_host_flow.gates import ASK_ATTEMPTS, MainChatGates, gate_answer, gate_form
from tests.flow_fakes import AWS


def sub(kind: str, value: Any) -> dict[str, Any]:
    return {"state": "submitted", "value": {"kind": kind, "value": value}}


class RecordingSink:
    """A `TurnSink` stand-in that records what the gates publish and answers from a script."""

    def __init__(self) -> None:
        self.outcomes: list[InputOutcome] = []
        self.requests: list[InputRequest] = []
        self.confirmations: list[ToolConfirmation] = []
        self.confirmed: list[tuple[str, bool, str | None]] = []
        self.resolved: list[tuple[str, str]] = []
        self.approve = True
        self.hang: asyncio.Event | None = None
        self.asked = asyncio.Event()

    async def _hang(self) -> None:
        self.asked.set()
        await asyncio.Event().wait()

    async def request_input(self, request: InputRequest) -> InputOutcome:
        self.requests.append(request)
        if self.hang is not None:
            await self._hang()
        return self.outcomes.pop(0)

    async def confirm_tool_call(self, c: ToolConfirmation) -> ToolConfirmationOutcome:
        self.confirmations.append(c)
        if self.hang is not None:
            await self._hang()
        return ToolConfirmationOutcome(approved=self.approve)

    async def tool_call_started(
        self,
        call_id: str,
        name: str,
        tool_input: Any,
        *,
        display_name: str | None = None,
        meta: Any = None,
    ) -> None:
        return None

    async def tool_call_completed(
        self,
        call_id: str,
        result: Any,
        *,
        success: bool,
        past_tense_message: str | None = None,
    ) -> None:
        return None

    async def tool_call_confirmed(
        self, call_id: str, *, approved: bool, reason_message: str | None = None
    ) -> None:
        self.confirmed.append((call_id, approved, reason_message))

    async def input_resolved(
        self, key: str, *, response: str = "accept", answers: Any = None
    ) -> bool:
        self.resolved.append((key, response))
        return True


def make(
    sink: RecordingSink, logs: list[str], working_directories: tuple[str, ...] = ()
) -> MainChatGates:
    return MainChatGates(
        sink=cast(TurnSink, sink),
        publisher=cast(SessionPublisher, object()),
        chat="chat:/main",
        redactor=Redactor(),
        on_log=logs.append,
        working_directories=working_directories,
    )


def test_flowext_gateform_choices() -> None:
    options = {"choices": ["yes", "no"]}
    assert gate_form("Ship?", options) == (
        None,
        [
            InputQuestion(
                "answer",
                "single-select",
                "Ship?",
                [{"id": "yes", "label": "yes"}, {"id": "no", "label": "no"}],
                {"required": True},
            )
        ],
    )
    assert gate_answer(options, {"answer": sub("selected", "no")}) == ("no", None)
    assert gate_answer(options, {"answer": sub("selected", "maybe")}) == (
        "maybe",
        "'maybe' is not one of the choices",
    )


def test_flowext_gateform_schema_enum() -> None:
    schema = {
        "type": "object",
        "properties": {"color": {"type": "string", "enum": ["red", "blue"], "title": "Color"}},
        "required": ["color"],
    }
    message, questions = gate_form("Q", {"schema": schema})
    assert message == "Q"
    (q,) = questions
    assert (q.id, q.kind, q.message) == ("color", "single-select", "Color")
    assert q.options == [{"id": "red", "label": "red"}, {"id": "blue", "label": "blue"}]
    assert q.extra == {"title": "Color", "required": True}


def test_flowext_gateform_number() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "number", "description": "How many"}}}
    _, (q,) = gate_form("Q", {"schema": schema})
    assert (q.id, q.kind, q.message, q.extra) == ("n", "number", "How many", {})
    assert gate_answer({"schema": schema}, {"n": sub("number", 2.5)}) == ({"n": 2.5}, None)


def test_flowext_gateform_multi_select() -> None:
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}},
    }
    _, (q,) = gate_form("Q", {"schema": schema})
    assert q.kind == "multi-select"
    assert q.options == [{"id": "a", "label": "a"}, {"id": "b", "label": "b"}]
    answers = {"tags": sub("selected", ["a", "b"])}
    assert gate_answer({"schema": schema}, answers) == ({"tags": ["a", "b"]}, None)


def test_flowext_gateform_json_fallback() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "object"}}, "required": ["x"]}
    message, questions = gate_form("Q", {"schema": schema})
    assert message is None
    (q,) = questions
    assert (q.id, q.kind) == ("answer", "text")
    assert q.message == "Q\n\nAnswer with JSON matching this schema:\n" + json.dumps(
        schema, indent=2
    )
    assert q.extra == {"required": True}
    assert gate_answer({"schema": schema}, {"answer": sub("text", '{"x": {}}')}) == (
        {"x": {}},
        None,
    )
    value, reason = gate_answer({"schema": schema}, {"answer": sub("text", "nope")})
    assert value == "nope"
    assert reason is not None
    assert reason.startswith("not JSON")


async def test_flowext_gateform_attempts_exhausted() -> None:
    sink = RecordingSink()
    logs: list[str] = []
    sink.outcomes = [InputOutcome("accept", {"answer": sub("selected", "maybe")})] * 3
    gates = make(sink, logs)
    assert await gates.ask(GateCall(0, "ask", "Ship?", {"choices": ["yes", "no"]})) is None
    assert [r.key for r in sink.requests] == ["gate-0-0", "gate-0-1", "gate-0-2"]
    assert (sink.requests[1].message or "").startswith("'maybe' is not one of the choices")
    assert logs == ["ask() #0: no valid answer after 3 attempts: 'maybe' is not one of the choices"]
    assert ASK_ATTEMPTS == 3


async def test_flowext_gateform_declined() -> None:
    sink = RecordingSink()
    logs: list[str] = []
    sink.outcomes = [InputOutcome("decline")]
    gates = make(sink, logs)
    assert await gates.ask(GateCall(0, "ask", "Ship?", {"choices": ["yes", "no"]})) is None
    assert len(sink.requests) == 1
    assert logs == []


async def test_flowext_gateform_title_masked() -> None:
    schema = {
        "type": "object",
        "properties": {"k": {"type": "string", "title": "key " + AWS, "description": "Key?"}},
    }
    sink = RecordingSink()
    logs: list[str] = []
    sink.outcomes = [InputOutcome("accept", {"k": sub("text", "v")})]
    gates = make(sink, logs)
    assert await gates.ask(GateCall(0, "ask", "Q", {"schema": schema})) == {"k": "v"}
    (q,) = sink.requests[0].questions
    assert "[redacted]" in q.extra["title"]
    assert AWS not in q.extra["title"]
    # Option ids must match the answers, so they stay as typed; only the labels are masked.
    sink.outcomes = [InputOutcome("accept", {"answer": sub("selected", AWS)})]
    assert await gates.ask(GateCall(1, "ask", "Pick", {"choices": [AWS, "no"]})) == AWS
    (choice,) = sink.requests[1].questions
    assert [o["id"] for o in choice.options] == [AWS, "no"]
    assert AWS not in choice.options[0]["label"]
    assert "[redacted]" in choice.options[0]["label"]


async def test_flowext_gateform_approve_string_diff() -> None:
    diff = "--- a/x\n+++ b/x\n-" + AWS + "\n"
    sink = RecordingSink()
    logs: list[str] = []
    sink.approve = True
    gates = make(sink, logs)
    assert await gates.approve(GateCall(0, "approve", "Deploy?", {"diff": diff})) is True
    (confirmation,) = sink.confirmations
    assert confirmation.invocation_message == (
        "Deploy?\n\n```diff\n" + Redactor().mask(diff) + "\n```"
    )
    assert AWS not in confirmation.invocation_message
    assert confirmation.edits == []


async def test_flowext_gateform_approve_list_diff(tmp_path: Path) -> None:
    diff = [{"path": "a.txt", "before": "x\n", "after": "y " + AWS}]
    sink = RecordingSink()
    logs: list[str] = []
    gates = make(sink, logs, (tmp_path.as_uri(),))
    assert await gates.approve(GateCall(0, "approve", "Go?", {"diff": diff})) is True
    (confirmation,) = sink.confirmations
    after = Redactor().mask("y " + AWS).encode("utf-8")
    assert confirmation.edits == [
        FileChange(uri=(tmp_path / "a.txt").as_uri(), before=b"x\n", after=after)
    ]
    assert AWS.encode() not in after


async def test_flowext_gateform_approve_no_workdir() -> None:
    diff = [{"path": "a.txt", "before": "x\n", "after": "y\n"}]
    sink = RecordingSink()
    logs: list[str] = []
    gates = make(sink, logs, ())
    await gates.approve(GateCall(0, "approve", "Go?", {"diff": diff}))
    assert sink.confirmations[0].edits == []
    assert logs == ["approve() #0: no local working directory; diff paths not previewed"]


# The two tests below prove the withdrawal on abort. In a hosted run the client's
# chat/turnCancelled is reduced before the provider task is cancelled, so the host drops the
# provider's chat/inputCompleted and chat/toolCallConfirmed as no-ops and no such frame can be
# seen on the wire; the sink call is the observable.


async def test_flowext_gateform_abort_withdraws_ask() -> None:
    sink = RecordingSink()
    logs: list[str] = []
    sink.hang = asyncio.Event()
    gates = make(sink, logs)
    task = asyncio.ensure_future(gates.ask(GateCall(4, "ask", "Wait?", {})))
    await sink.asked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sink.resolved == [("gate-4-0", "cancel")]


async def test_flowext_gateform_abort_withdraws_approve() -> None:
    sink = RecordingSink()
    logs: list[str] = []
    sink.hang = asyncio.Event()
    gates = make(sink, logs)
    task = asyncio.ensure_future(gates.approve(GateCall(2, "approve", "Go?", {})))
    await sink.asked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sink.confirmed == [("gate-2", False, "run aborted")]
