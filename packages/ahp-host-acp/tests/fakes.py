"""A recording `TurnSink`, and a helper to run the fake ACP agent."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ahp_host.provider.base import (
    AuthChallenge,
    ClientToolCall,
    InputOutcome,
    InputRequest,
    ToolConfirmation,
    ToolConfirmationOutcome,
    ToolResult,
)

#: Runs the scripted agent with this interpreter.
FAKE_AGENT = (sys.executable, str(Path(__file__).with_name("fake_agent.py")))


class RecordingSink:
    def __init__(self, approve: bool = True, edited_input: Any = None) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.approve = approve
        self.edited_input = edited_input
        self.confirmations: list[ToolConfirmation] = []
        #: The latest line under each call's name, and each call's past tense.
        self.invocations: dict[str, str] = {}
        self.past_tense: dict[str, str | None] = {}
        self.outputs: dict[str, list[Mapping[str, Any]]] = {}

    async def text_delta(self, text: str) -> None:
        self.events.append(("text", text))

    async def reasoning_delta(self, text: str) -> None:
        self.events.append(("reasoning", text))

    async def tool_call_started(
        self,
        call_id: str,
        name: str,
        tool_input: Any = None,
        *,
        display_name: str | None = None,
        intention: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        self.events.append(("started", call_id, name, display_name))

    async def tool_call_delta(
        self,
        call_id: str,
        content: str | None = None,
        *,
        invocation_message: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        if invocation_message is not None:
            self.invocations[call_id] = invocation_message
        self.events.append(("delta", call_id))

    async def tool_call_output(
        self,
        call_id: str,
        content: Sequence[Mapping[str, Any]],
        *,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        self.outputs[call_id] = list(content)
        self.events.append(("output", call_id))

    async def tool_call_confirmed(
        self, call_id: str, *, approved: bool, reason_message: str | None = None
    ) -> None:
        self.events.append(("confirmed_elsewhere", call_id))

    async def usage(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        model: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        self.events.append(("usage", input_tokens, output_tokens, cache_read_tokens, model))

    async def tool_call_completed(
        self,
        call_id: str,
        result: Any = None,
        *,
        success: bool = True,
        past_tense_message: str | None = None,
    ) -> None:
        self.past_tense[call_id] = past_tense_message
        self.events.append(("completed", call_id, success, result))

    async def turn_failed(
        self, message: str, error_type: str = "agent.turn", duration_ms: int = 0
    ) -> None:
        self.events.append(("failed", message, error_type))

    async def request_input(self, request: InputRequest) -> InputOutcome:
        raise AssertionError("not used")

    async def confirm_tool_call(self, call: ToolConfirmation) -> ToolConfirmationOutcome:
        self.confirmations.append(call)
        self.events.append(("confirm", call.call_id))
        return ToolConfirmationOutcome(
            approved=self.approve,
            tool_input=self.edited_input if self.edited_input is not None else call.tool_input,
        )

    async def request_authentication(self, call_id: str, challenge: AuthChallenge) -> None:
        raise AssertionError("not used")

    async def run_client_tool(self, call: ClientToolCall) -> ToolResult:
        raise AssertionError("not used")
