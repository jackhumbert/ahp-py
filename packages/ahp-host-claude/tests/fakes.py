"""A scripted stand-in for `ClaudeSDKClient`, and a recording `TurnSink`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
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
from claude_agent_sdk import UserMessage as SdkUserMessage

#: A script step is a message to yield, or a coroutine function run in place
#: (how a test makes the CLI "ask permission" mid-stream).
Step = Any | Callable[[ClaudeAgentOptions], Awaitable[None]]


class FakeClient:
    """Claude Code, scripted.

    `turns` is what the CLI says in answer to each `query`, in order: sending a
    message queues the next script onto the one stream (`receive_messages`),
    as the real CLI's output is one stream for the life of the client. `push`
    queues messages nobody here asked for - a turn typed on claude.ai.
    """

    def __init__(self, options: ClaudeAgentOptions, turns: list[list[Step]]) -> None:
        self.options = options
        self.turns = turns
        self.prompts: list[Any] = []
        self.server_info: dict[str, Any] | None = None
        self.models: list[str | None] = []
        self.permission_modes: list[Any] = []
        self.remote_controls: list[tuple[bool, str | None]] = []
        self.keeps: list[bool] = []
        #: What `remote_control(True)` answers; an exception is raised.
        self.bridge_reply: Mapping[str, Any] | Exception = {
            "session_url": "https://claude.ai/code/session_1",
            "bridge_session_id": "cse_1",
        }
        self.connected = False
        self.interrupted = False
        self.disconnected = False
        self._stream: asyncio.Queue[Step] = asyncio.Queue()

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str | AsyncIterable[dict[str, Any]]) -> None:
        if isinstance(prompt, str):
            self.prompts.append(prompt)
        else:
            messages = [message async for message in prompt]
            self.prompts.append(messages)
        if self.turns:
            self.push(*self.turns.pop(0))

    def push(self, *steps: Step) -> None:
        for step in steps:
            self._stream.put_nowait(step)

    async def get_server_info(self) -> dict[str, Any] | None:
        return self.server_info

    async def receive_messages(self) -> AsyncIterator[Any]:
        while True:
            step = await self._stream.get()
            if callable(step):
                await step(self.options)
            else:
                yield self._echoed(step)

    def _echoed(self, step: Any) -> Any:
        """A scripted replay of a message we sent carries its uuid, as the CLI's does."""
        if not isinstance(step, SdkUserMessage) or step.uuid is not None or step.origin:
            return step
        for prompt in reversed(self.prompts):
            if isinstance(prompt, list) and text_of(prompt) == step.content:
                return replace(step, uuid=prompt[0].get("uuid"))
        return step

    async def interrupt(self) -> None:
        self.interrupted = True

    async def set_model(self, model: str | None = None) -> None:
        self.models.append(model)

    async def set_permission_mode(self, mode: Any) -> None:
        self.permission_modes.append(mode)

    async def remote_control(
        self, enabled: bool, *, reattach: str | None = None, keep: bool = True
    ) -> Mapping[str, Any]:
        self.remote_controls.append((enabled, reattach))
        self.keeps.append(keep)
        if not enabled:
            return {}
        if isinstance(self.bridge_reply, Exception):
            raise self.bridge_reply
        return self.bridge_reply

    async def disconnect(self) -> None:
        self.disconnected = True


def text_of(prompt: Any) -> Any:
    """What a recorded prompt said: its text, or its content blocks."""
    if isinstance(prompt, list) and len(prompt) == 1 and isinstance(prompt[0], dict):
        return prompt[0]["message"]["content"]
    return prompt


class RecordingSink:
    def __init__(self, approve: bool = True, edited_input: Any = None) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.approve = approve
        self.edited_input = edited_input
        self.confirmations: list[ToolConfirmation] = []
        #: Set to keep approval prompts unanswered, as when a phone answers.
        self.hold: asyncio.Event | None = None
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
        if self.hold is not None:
            await self.hold.wait()  # nobody here answers
        return ToolConfirmationOutcome(
            approved=self.approve,
            tool_input=self.edited_input if self.edited_input is not None else call.tool_input,
        )

    async def tool_call_confirmed(
        self, call_id: str, *, approved: bool, reason_message: str | None = None
    ) -> None:
        self.events.append(("confirmed_elsewhere", call_id, approved, reason_message))

    async def request_authentication(self, call_id: str, challenge: AuthChallenge) -> None:
        raise AssertionError("not used")

    async def run_client_tool(self, call: ClientToolCall) -> ToolResult:
        raise AssertionError("not used")


class FakePublisher:
    """The host's out-of-turn side: only `external_turn` does anything.

    Runs each external turn the way the host does - on its own task, with a
    fresh sink - and refuses one while another is running.
    """

    def __init__(self) -> None:
        self.turns: list[tuple[str, RecordingSink]] = []
        self.tasks: list[asyncio.Task[None]] = []
        self.refusals = 0
        self.config_changes: list[dict[str, Any]] = []

    async def external_turn(self, text: str, run: Callable[[Any], Awaitable[None]]) -> bool:
        if any(not task.done() for task in self.tasks):
            self.refusals += 1
            return False
        sink = RecordingSink()
        self.turns.append((text, sink))

        async def turn() -> None:
            await run(sink)

        self.tasks.append(asyncio.create_task(turn()))
        return True

    async def customizations_changed(
        self,
        customizations: Sequence[Mapping[str, Any]],
        server_tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        return

    async def activity_changed(self, activity: str | None) -> None:
        return

    async def changes_published(self, changeset: Any, changes: Sequence[Any]) -> str:
        return ""

    async def mcp_server_changed(
        self, customization_id: str, state: Mapping[str, Any], channel: str | None = None
    ) -> None:
        return

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        return

    async def title_changed(self, title: str) -> None:
        return

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        self.config_changes.append(dict(values))


async def eventually(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait for something the adapter does on its own task."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)
