"""A scripted stand-in for `ClaudeSDKClient`, and a recording `TurnSink`."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

from agent_host_server.provider.base import (
    AuthChallenge,
    ClientToolCall,
    InputOutcome,
    InputRequest,
    ToolConfirmation,
    ToolConfirmationOutcome,
    ToolResult,
)
from claude_agent_sdk import ClaudeAgentOptions

#: A script step is a message to yield, or a coroutine function run in place
#: (how a test makes the CLI "ask permission" mid-stream).
Step = Any | Callable[[ClaudeAgentOptions], Awaitable[None]]


class FakeClient:
    def __init__(self, options: ClaudeAgentOptions, turns: list[list[Step]]) -> None:
        self.options = options
        self.turns = turns
        self.prompts: list[Any] = []
        self.server_info: dict[str, Any] | None = None
        self.models: list[str | None] = []
        self.permission_modes: list[Any] = []
        self.connected = False
        self.interrupted = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str | AsyncIterable[dict[str, Any]]) -> None:
        if isinstance(prompt, str):
            self.prompts.append(prompt)
        else:
            self.prompts.append([message async for message in prompt])

    async def get_server_info(self) -> dict[str, Any] | None:
        return self.server_info

    async def receive_response(self) -> AsyncIterator[Any]:
        for step in self.turns.pop(0):
            if callable(step):
                await step(self.options)
            else:
                yield step

    async def interrupt(self) -> None:
        self.interrupted = True

    async def set_model(self, model: str | None = None) -> None:
        self.models.append(model)

    async def set_permission_mode(self, mode: Any) -> None:
        self.permission_modes.append(mode)

    async def disconnect(self) -> None:
        self.disconnected = True


class RecordingSink:
    def __init__(self, approve: bool = True, edited_input: Any = None) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.approve = approve
        self.edited_input = edited_input
        self.confirmations: list[ToolConfirmation] = []
        #: The latest line under each call's name, and each call's past tense.
        self.invocations: dict[str, str] = {}
        self.past_tense: dict[str, str | None] = {}

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
        self.events.append(("output", call_id))

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
