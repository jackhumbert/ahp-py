"""`RemoteWorkerChats`: the thin worker chat of a remote `agent()` call (plan §4.4)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ahp_host import TurnSink
from ahp_host.provider.base import (
    ClientToolCall,
    InputQuestion,
    InputRequest,
    ToolConfirmation,
)
from ahp_protocol.ext import flow

from ahp_host_flow import dispatch
from ahp_host_flow.bridge import TurnTee
from ahp_host_flow.clienttools import ClientTools
from ahp_host_flow.dispatch import FlowAgentError, StepDispatcher, _Worker
from ahp_host_flow.engine import (
    AgentCall,
    AgentResult,
    DispatchError,
    DispatchHandle,
    Redactor,
    RemoteAnswer,
    RemoteConfirmation,
    RemoteDecision,
    RemoteOut,
    RemoteQuestion,
    report_dispatch,
)
from ahp_host_flow.engine.remote import RemoteToolError


class _WorkerOut:
    """A remote call's output, written to its worker chat."""

    def __init__(
        self,
        sink: TurnSink,
        provider: str,
        chat: str,
        redactor: Redactor,
        tools: ClientTools | None = None,
    ) -> None:
        self.sink = sink
        self.provider = provider
        self.chat = chat
        self.redactor = redactor
        self.client_tools = tools

    def tools(self) -> list[dict[str, Any]]:
        return self.client_tools.definitions() if self.client_tools is not None else []

    async def run_tool(self, name: str, tool_input: Any, tool_call_id: str) -> Mapping[str, Any]:
        owner = self.client_tools.owner(name) if self.client_tools is not None else None
        if owner is None:
            raise RemoteToolError(f"no client of the run offers {name!r}")
        try:
            result = await self.sink.run_client_tool(
                ClientToolCall(
                    call_id=f"remote-{tool_call_id}",
                    name=name,
                    client_id=owner,
                    tool_input=tool_input,
                    display_name=name,
                )
            )
        except LookupError as exc:
            raise RemoteToolError(str(exc)) from exc
        if not result.accepted:
            raise RemoteToolError(result.reason or f"the client answered {result.response}")
        if isinstance(result.value, Mapping):
            return result.value
        return {
            "success": True,
            "pastTenseMessage": f"Ran {name}",
            "content": [{"type": "text", "text": json.dumps(result.value)}],
        }

    async def linked(self, session_uri: str) -> None:
        await self.sink.text_delta(f"Remote session: {session_uri}\n")
        report_dispatch(DispatchHandle(provider=self.provider, chat=self.chat, session=session_uri))

    async def text(self, delta: str) -> None:
        await self.sink.text_delta(self.redactor.mask(delta))

    # No approval rules apply to relayed remote requests: the worker chat's user decides.
    async def confirm(self, request: RemoteConfirmation) -> RemoteDecision | None:
        tee = TurnTee(self.sink, redactor=self.redactor)
        name = request.tool_name or "tool"
        await tee.tool_call_started(
            request.tool_call_id,
            name,
            request.tool_input,
            display_name=request.tool_name or None,
        )
        outcome = await tee.confirm_tool_call(
            ToolConfirmation(
                call_id=request.tool_call_id,
                name=name,
                invocation_message=request.invocation_message
                or f"Run {request.tool_name or 'a tool'} on the remote host",
                tool_input=request.tool_input,
                confirmation_title=request.confirmation_title,
                read_only=request.read_only,
            )
        )
        await tee.tool_call_completed(
            request.tool_call_id,
            None,
            success=outcome.approved,
            past_tense_message="Approved" if outcome.approved else "Denied",
        )
        suggestion: dict[str, Any] | None = None
        given = outcome.user_suggestion
        if given is not None:
            if isinstance(given.raw, Mapping) and given.raw:
                suggestion = dict(given.raw)
            elif given.text:
                suggestion = {"text": given.text}
        return RemoteDecision(outcome.approved, outcome.reason_message, suggestion)

    async def ask(self, request: RemoteQuestion) -> RemoteAnswer | None:
        tee = TurnTee(self.sink, redactor=self.redactor)
        outcome = await tee.request_input(
            InputRequest(
                message=request.message,
                url=request.url,
                questions=[
                    InputQuestion(
                        id=q["id"],
                        kind=str(q.get("kind", "text")),
                        message=str(q.get("message", "")),
                        options=list(q.get("options") or []),
                        extra={
                            k: v
                            for k, v in q.items()
                            if k not in ("id", "kind", "message", "options")
                        },
                    )
                    for q in request.questions
                    if isinstance(q.get("id"), str)
                ],
            )
        )
        return RemoteAnswer(outcome.response, dict(outcome.answers))


class RemoteWorkerChats(StepDispatcher):
    """Shows each remote `agent()` call as a thin worker chat: the remote session and its text."""

    def __init__(self, *, client_tools: ClientTools | None = None, **options: Any) -> None:
        super().__init__(**options)
        self.client_tools = client_tools
        #: The body of each call in flight, by ordinal; `run` hands it to `_run_call`.
        self._bodies: dict[int, Callable[[RemoteOut, str], Awaitable[AgentResult]]] = {}

    async def run(
        self,
        call: AgentCall,
        provider: str,
        body: Callable[[RemoteOut, str], Awaitable[AgentResult]],
    ) -> AgentResult:
        self._bodies[call.ordinal] = body
        try:
            return await self.run_agent(call)
        finally:
            self._bodies.pop(call.ordinal, None)

    async def _run_call(self, call: AgentCall, name: str, title: str, call_id: str) -> AgentResult:
        body = self._bodies[call.ordinal]
        worker = await self.publisher.open_tool_chat(title, tool_call_id=call_id, chat=self.chat)
        raw_schema = call.opts.get("schema")
        started = self._steps[call_id]
        started["chat"] = worker.resource
        link_item = {"type": "subagent", "resource": worker.resource, "title": title}
        # Clients can open the worker chat while it runs.
        await self.sink.tool_call_output(call_id, [link_item], meta=flow.meta(flow.step(started)))
        self._watch(call_id, link_item)

        state = _Worker()
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        value: Any = None

        async def run(sink: TurnSink) -> None:
            nonlocal value
            state.started = True
            state.task = asyncio.current_task()

            try:
                result = await body(
                    _WorkerOut(sink, name, worker.resource, self.redactor, self.client_tools), title
                )
                for key in usage:
                    count = result.usage.get(key)
                    usage[key] = count if isinstance(count, int) else 0
                await sink.usage(
                    input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]
                )
                value = self.redactor.mask_value(result.value)
            except asyncio.CancelledError:
                if not state.aborting:
                    state.skipped = True
                raise
            except Exception as exc:
                state.error = self.redactor.mask(str(exc))
                if isinstance(exc, DispatchError):
                    state.kind, state.transient = exc.kind, exc.transient
                raise
            finally:
                state.done.set()

        def turn_ended() -> None:
            # If `run` never started, the turn was cancelled before its first step: a skip.
            if not state.started:
                state.skipped = True
            state.done.set()

        try:
            if not await worker.run_turn(call.prompt, run, on_end=turn_ended):
                raise FlowAgentError("could not start a turn in the worker chat")
            await state.done.wait()
        except asyncio.CancelledError:
            # The run was aborted (a worker skip cancels only the callback).
            state.aborting = True
            if state.task is not None and not state.task.done():
                state.task.cancel()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(state.done.wait(), dispatch.ABORT_GRACE)
            raise

        return await self._finish(
            call_id,
            link_item,
            usage=usage,
            value=value,
            raw_schema=raw_schema,
            skipped=state.skipped,
            error=state.error,
            kind=state.kind,
            transient=state.transient,
        )
