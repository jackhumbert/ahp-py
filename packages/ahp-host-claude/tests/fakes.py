"""A scripted stand-in for `ClaudeSDKClient`, and a recording `TurnSink`."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from ahp_host.provider.base import (
    AuthChallenge,
    BackgroundWork,
    Canvas,
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
        #: Raised by `connect`, as the CLI refusing to start would be.
        self.connect_error: Exception | None = None
        #: `get_mcp_status()["mcpServers"]`, and what was asked of them.
        self.mcp_servers: list[dict[str, Any]] = []
        self.mcp_calls: list[tuple[str, str, bool | None]] = []
        #: What `get_context_usage` answers for each model `set_model` picked.
        self.context_limits: dict[str | None, dict[str, Any]] = {}
        #: `file_suggestions`: what it answers, and what it was asked.
        self.suggestions: list[dict[str, Any]] = []
        self.suggestion_cwd: str | None = None
        self.suggestion_queries: list[str] = []
        self.stopped_tasks: list[str] = []
        self._stream: asyncio.Queue[Step] = asyncio.Queue()

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
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

    async def get_mcp_status(self) -> dict[str, Any]:
        return {"mcpServers": [dict(server) for server in self.mcp_servers]}

    async def toggle_mcp_server(self, server_name: str, enabled: bool) -> None:
        self.mcp_calls.append(("toggle", server_name, enabled))
        for server in self.mcp_servers:
            if server["name"] == server_name:
                server["status"] = "connected" if enabled else "disabled"

    async def reconnect_mcp_server(self, server_name: str) -> None:
        self.mcp_calls.append(("reconnect", server_name, None))
        for server in self.mcp_servers:
            if server["name"] == server_name:
                server["status"] = "connected"

    async def context_usage(self) -> dict[str, Any]:
        model = self.models[-1] if self.models else None
        if model not in self.context_limits:
            raise RuntimeError(f"no limits scripted for {model!r}")
        return dict(self.context_limits[model])

    async def file_suggestions(self, query: str) -> dict[str, Any]:
        self.suggestion_queries.append(query)
        reply: dict[str, Any] = {"suggestions": list(self.suggestions)}
        if self.suggestion_cwd is not None:
            reply["cwd"] = self.suggestion_cwd
        return reply

    async def stop_task(self, task_id: str) -> None:
        self.stopped_tasks.append(task_id)


def text_of(prompt: Any) -> Any:
    """What a recorded prompt said: its text, or its content blocks."""
    if isinstance(prompt, list) and len(prompt) == 1 and isinstance(prompt[0], dict):
        return prompt[0]["message"]["content"]
    return prompt


class RecordingSink:
    def __init__(
        self, approve: bool = True, edited_input: Any = None, turn_id: str | None = None
    ) -> None:
        #: The AHP turn this sink publishes into, as the host's sink knows it.
        self.turn_id = turn_id if turn_id is not None else f"turn-{uuid.uuid4()}"
        self.events: list[tuple[Any, ...]] = []
        self.approve = approve
        self.edited_input = edited_input
        self.confirmations: list[ToolConfirmation] = []
        #: Set to keep approval prompts unanswered, as when a phone answers.
        self.hold: asyncio.Event | None = None
        #: The latest line under each call's name, and each call's past tense.
        self.invocations: dict[str, str] = {}
        self.past_tense: dict[str, str | None] = {}
        #: Client tools the adapter asked for, and what the client answers.
        self.client_calls: list[ClientToolCall] = []
        self.client_result: ToolResult | Exception = ToolResult(
            value={"success": True, "content": [{"type": "text", "text": "ran"}]}
        )
        #: Input requests asked, and how they are answered (`None`: never).
        self.inputs: list[InputRequest] = []
        self.input_outcome: InputOutcome | None = InputOutcome(response="cancel")
        #: Each usage report's `_meta`.
        self.usage_meta: list[Mapping[str, Any] | None] = []
        self.notifications: list[str] = []
        self.notification_meta: list[Mapping[str, Any] | None] = []
        #: Whether each failure was offered for resuming.
        self.resumable: list[bool] = []
        #: Every change handed to `file_edit`.
        self.file_edits: list[Any] = []
        #: What `confirm_tool_call` answers, instead of approve/edited_input.
        self.outcome: ToolConfirmationOutcome | None = None

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
        self.usage_meta.append(meta)
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

    async def file_edit(self, change: Any) -> Mapping[str, Any]:
        self.file_edits.append(change)
        return {"type": "fileEdit", "uri": change.uri}

    async def system_notification(
        self, text: str, *, markdown: bool = False, meta: Mapping[str, Any] | None = None
    ) -> None:
        self.notifications.append(text)
        self.notification_meta.append(meta)

    async def turn_failed(
        self,
        message: str,
        error_type: str = "agent.turn",
        duration_ms: int = 0,
        *,
        resumable: bool = False,
    ) -> None:
        self.resumable.append(resumable)
        self.events.append(("failed", message, error_type))

    async def request_input(self, request: InputRequest) -> InputOutcome:
        self.inputs.append(request)
        self.events.append(("input", len(request.questions)))
        if self.hold is not None:
            await self.hold.wait()  # nobody here answers
        if self.input_outcome is None:
            await asyncio.Event().wait()
        assert self.input_outcome is not None
        return self.input_outcome

    async def confirm_tool_call(self, call: ToolConfirmation) -> ToolConfirmationOutcome:
        self.confirmations.append(call)
        self.events.append(("confirm", call.call_id))
        if self.hold is not None:
            await self.hold.wait()  # nobody here answers
        if self.outcome is not None:
            return self.outcome
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
        self.client_calls.append(call)
        self.events.append(("client_tool", call.call_id, call.name, call.client_id))
        if isinstance(self.client_result, Exception):
            raise self.client_result
        return self.client_result


class ResolvingSink(RecordingSink):
    """The host's sink can withdraw an input request answered elsewhere."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.resolved: list[tuple[str, str, Mapping[str, Any] | None]] = []

    async def input_resolved(
        self, key: str, *, response: str = "accept", answers: Mapping[str, Any] | None = None
    ) -> bool:
        self.resolved.append((key, response, answers))
        return True


class FakeChat:
    """A `ProviderChat`: each worker turn runs on its own task, into a fresh sink."""

    def __init__(self, resource: str, title: str, tool_call_id: str) -> None:
        self.resource = resource
        self.title = title
        self.tool_call_id = tool_call_id
        self.prompts: list[str] = []
        self.sinks: list[RecordingSink] = []
        self.tasks: list[asyncio.Task[None]] = []
        #: The chat it was opened under (None: the default chat).
        self.parent: str | None = None

    async def run_turn(self, text: str, run: Callable[[Any], Awaitable[None]]) -> bool:
        if any(not task.done() for task in self.tasks):
            return False
        sink = RecordingSink()
        self.prompts.append(text)
        self.sinks.append(sink)

        async def turn() -> None:
            await run(sink)

        self.tasks.append(asyncio.create_task(turn()))
        return True


class FakeTerminal:
    """A `ProviderTerminal` that records what was written."""

    def __init__(self, resource: str, title: str) -> None:
        self.resource = resource
        self.title = title
        self.output: list[str] = []
        self.exit_code: int | None = None
        self.done = False

    async def write(self, data: str) -> None:
        self.output.append(data)

    async def exited(self, exit_code: int | None = None) -> None:
        self.done, self.exit_code = True, exit_code


class FakePublisher:
    """The host's out-of-turn side: only `external_turn` does anything.

    Runs each external turn the way the host does - on its own task, with a
    fresh sink - and refuses one while another is running.
    """

    def __init__(self) -> None:
        self.turns: list[tuple[str, RecordingSink]] = []
        #: The `chat=` each external turn named (None: the default chat).
        self.external_chats: list[str | None] = []
        self.tasks: list[asyncio.Task[None]] = []
        self.refusals = 0
        self.config_changes: list[dict[str, Any]] = []
        #: The chat's background work as the host would hold it, by id, and
        #: which chat each was published for (None: the default chat).
        self.background: dict[str, dict[str, Any]] = {}
        self.background_chats: dict[str, str | None] = {}
        self.terminals: list[FakeTerminal] = []
        self.chats: list[FakeChat] = []
        #: Every customization tree published, and each MCP server's lifecycle.
        self.trees: list[list[dict[str, Any]]] = []
        self.mcp_states: list[tuple[str, dict[str, Any]]] = []
        #: Each changeset publication: (changeset, changes, chat).
        self.changesets: list[tuple[Any, list[Any], str | None]] = []

    async def external_turn(
        self, text: str, run: Callable[[Any], Awaitable[None]], *, chat: str | None = None
    ) -> bool:
        if any(not task.done() for task in self.tasks):
            self.refusals += 1
            return False
        sink = RecordingSink()
        self.turns.append((text, sink))
        self.external_chats.append(chat)

        async def turn() -> None:
            await run(sink)

        self.tasks.append(asyncio.create_task(turn()))
        return True

    async def customizations_changed(
        self,
        customizations: Sequence[Mapping[str, Any]],
        server_tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.trees.append([dict(c) for c in customizations])

    async def activity_changed(self, activity: str | None) -> None:
        return

    async def changes_published(
        self, changeset: Any, changes: Sequence[Any], *, chat: str | None = None
    ) -> str:
        self.changesets.append((changeset, list(changes), chat))
        return str(changeset.uri)

    async def mcp_server_changed(
        self, customization_id: str, state: Mapping[str, Any], channel: str | None = None
    ) -> None:
        self.mcp_states.append((customization_id, dict(state)))

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        return

    async def title_changed(self, title: str) -> None:
        return

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        self.config_changes.append(dict(values))

    async def open_tool_chat(
        self,
        title: str,
        *,
        tool_call_id: str,
        chat: str | None = None,
        interactivity: str = "read-only",
    ) -> FakeChat:
        opened = FakeChat(f"ahp-chat:/worker-{len(self.chats)}", title, tool_call_id)
        opened.parent = chat
        self.chats.append(opened)
        return opened

    async def canvas_set(self, canvas: Canvas, *, chat: str | None = None) -> str:
        return f"ahp-canvas:/{canvas.instance_id}"

    async def canvas_removed(self, instance_id: str, *, chat: str | None = None) -> None:
        return

    async def open_terminal(
        self,
        title: str,
        *,
        chat: str | None = None,
        cwd: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> FakeTerminal:
        terminal = FakeTerminal(f"ahp-terminal:/{len(self.terminals)}", title)
        self.terminals.append(terminal)
        return terminal

    async def background_work_set(self, work: BackgroundWork, *, chat: str | None = None) -> None:
        self.background[work.id] = work.to_wire()
        self.background_chats[work.id] = chat

    async def background_work_removed(self, work_id: str, *, chat: str | None = None) -> None:
        self.background.pop(work_id, None)


async def eventually(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait for something the adapter does on its own task."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)
