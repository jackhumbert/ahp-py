"""A recording `TurnSink` and `SessionPublisher`, and a helper to run the fake ACP agent."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from ahp_host.provider.base import (
    AgentSessionContext,
    AuthChallenge,
    ClientToolCall,
    InputOutcome,
    InputRequest,
    ToolConfirmation,
    ToolConfirmationOutcome,
    ToolResult,
)
from ahp_host.provider.changes import Changeset, FileChange

#: Runs the scripted agent with this interpreter.
FAKE_AGENT = (sys.executable, str(Path(__file__).with_name("fake_agent.py")))


class RecordingSink:
    def __init__(
        self,
        approve: bool = True,
        edited_input: Any = None,
        *,
        pick: str | None = None,
        reason_message: str | None = None,
    ) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.approve = approve
        self.edited_input = edited_input
        #: The confirmation option a user picks, by id; None answers plainly.
        self.pick = pick
        self.reason_message = reason_message
        self.file_edits: list[FileChange] = []
        self.confirmations: list[ToolConfirmation] = []
        #: The latest line under each call's name, and each call's past tense.
        self.invocations: dict[str, str] = {}
        self.past_tense: dict[str, str | None] = {}
        self.outputs: dict[str, list[Mapping[str, Any]]] = {}
        self.usage_meta: list[Mapping[str, Any] | None] = []

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
        self.usage_meta.append(meta)

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
        self,
        message: str,
        error_type: str = "agent.turn",
        duration_ms: int = 0,
        *,
        resumable: bool = False,
    ) -> None:
        self.events.append(("failed", message, error_type))

    async def file_edit(self, change: FileChange) -> Mapping[str, Any]:
        """A stand-in for the host's item: the real one carries `ContentRef`s."""
        self.file_edits.append(change)
        return {"type": "fileEdit", "after": {"uri": change.uri}}

    async def system_notification(
        self, text: str, *, markdown: bool = False, meta: Mapping[str, Any] | None = None
    ) -> None:
        raise AssertionError("not used")

    async def request_input(self, request: InputRequest) -> InputOutcome:
        raise AssertionError("not used")

    async def confirm_tool_call(self, call: ToolConfirmation) -> ToolConfirmationOutcome:
        self.confirmations.append(call)
        self.events.append(("confirm", call.call_id))
        picked = next((o for o in call.options if o.id == self.pick), None)
        return ToolConfirmationOutcome(
            approved=picked.kind == "approve" if picked is not None else self.approve,
            tool_input=self.edited_input if self.edited_input is not None else call.tool_input,
            selected_option=picked,
            reason=None if self.approve else "denied",
            reason_message=self.reason_message,
        )

    async def request_authentication(self, call_id: str, challenge: AuthChallenge) -> None:
        raise AssertionError("not used")

    async def run_client_tool(self, call: ClientToolCall) -> ToolResult:
        raise AssertionError("not used")


class RecordingPublisher:
    """The `SessionPublisher` calls this adapter makes, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.changesets: list[tuple[Changeset, list[FileChange]]] = []

    async def title_changed(self, title: str) -> None:
        self.events.append(("title", title))

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        self.events.append(("config", dict(values)))

    async def activity_changed(self, activity: str | None) -> None:
        self.events.append(("activity", activity))

    async def changes_published(
        self, changeset: Any, changes: Sequence[Any], *, chat: str | None = None
    ) -> str:
        self.changesets.append((changeset, list(changes)))
        self.events.append(("changes", changeset.uri))
        return str(changeset.uri)

    def of(self, kind: str) -> list[Any]:
        """The payloads of one kind of call."""
        return [event[1] for event in self.events if event[0] == kind]


def context(
    root: Path, publisher: RecordingPublisher | None = None, **kwargs: Any
) -> AgentSessionContext:
    """A session context in *root*, publishing to *publisher*."""
    return AgentSessionContext(
        session_uri="ahp-session:/1",
        chat_uri="ahp-chat:/1",
        provider_id="acp",
        working_directories=(root.as_uri(),),
        # Structurally a SessionPublisher for what this adapter calls; the
        # protocol itself is larger and still growing.
        publisher=cast(Any, publisher),
        **kwargs,
    )
