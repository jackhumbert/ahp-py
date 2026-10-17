"""Bridges an inner provider session into a worker chat (plan section 8, M0 spike notes)."""

from __future__ import annotations

import dataclasses
import inspect
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from ahp_host import TurnSink
from ahp_host.provider.base import (
    BackgroundWork,
    Canvas,
    InputOutcome,
    InputRequest,
    ProviderChat,
    ProviderTerminal,
    SessionPublisher,
    ToolConfirmation,
    ToolConfirmationOutcome,
)

from ahp_host_flow.engine import Redactor

#: Most drop records kept: an inner session reporting progress in a loop must
#: not grow memory without bound.
DROPPED_KEEP: Final = 256


class ScopedPublisher:
    """An outer `SessionPublisher` for an inner session whose world is one worker chat.

    The inner session is not trusted with the run. A `chat=` it passes is
    honoured only when it names the worker chat or a tool chat this publisher
    opened (recursively); any other chat -- the run's main chat, another
    worker -- is foreign. A foreign chat never reaches the outer publisher:
    calls that return nothing meaningful (config, turns, changes) are dropped,
    and calls that make something (canvases, terminals, tool chats, background
    work) are redirected to the worker chat.
    """

    def __init__(self, outer: SessionPublisher, chat: str) -> None:
        self.outer = outer
        self.chat = chat
        #: Chats this publisher opened; the inner session may address these.
        self._owned: set[str] = {chat}
        #: The last `DROPPED_KEEP` calls stopped here (session-wide calls an
        #: inner session must not make, and calls that named a foreign chat).
        self.dropped: deque[str] = deque(maxlen=DROPPED_KEEP)
        #: Every call stopped here, by kind; unbounded in time, bounded in size.
        self.dropped_counts: Counter[str] = Counter()

    def _drop(self, kind: str) -> None:
        self.dropped.append(kind)
        self.dropped_counts[kind] += 1

    def _own(self, chat: str | None, kind: str) -> str | None:
        """*chat* if the inner session may address it, else None (dropped)."""
        if chat is None or chat in self._owned:
            return chat if chat is not None else self.chat
        self._drop(f"foreign_chat:{kind}")
        return None

    def _redirect(self, chat: str | None, kind: str) -> str:
        """*chat* if the inner session may address it, else the worker chat."""
        return self._own(chat, kind) or self.chat

    async def customizations_changed(
        self,
        customizations: Sequence[Mapping[str, Any]],
        server_tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self._drop("customizations_changed")

    async def activity_changed(self, activity: str | None) -> None:
        self._drop("activity_changed")

    async def changes_published(
        self, changeset: Any, changes: Sequence[Any], *, chat: str | None = None
    ) -> str:
        target = self._own(chat, "changes_published")
        if target is None:
            return ""
        return await self.outer.changes_published(changeset, changes, chat=target)

    async def mcp_server_changed(
        self, customization_id: str, state: Mapping[str, Any], channel: str | None = None
    ) -> None:
        self._drop("mcp_server_changed")

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self._drop("progress")

    async def title_changed(self, title: str) -> None:
        self._drop("title_changed")

    async def config_changed(self, values: Mapping[str, Any], *, chat: str | None = None) -> None:
        # AHP 1.1.0 gives chats their own config overlay, so a change the inner
        # session reports lands on its worker chat; the run's own config is
        # never touched, nor is any chat that is not this worker's.
        target = self._own(chat, "config_changed")
        if target is not None:
            await self.outer.config_changed(values, chat=target)

    async def background_work_set(self, work: BackgroundWork, *, chat: str | None = None) -> None:
        await self.outer.background_work_set(work, chat=self._redirect(chat, "background_work_set"))

    async def background_work_removed(self, work_id: str, *, chat: str | None = None) -> None:
        await self.outer.background_work_removed(
            work_id, chat=self._redirect(chat, "background_work_removed")
        )

    async def canvas_set(self, canvas: Canvas, *, chat: str | None = None) -> str:
        return await self.outer.canvas_set(canvas, chat=self._redirect(chat, "canvas_set"))

    async def canvas_removed(self, instance_id: str, *, chat: str | None = None) -> None:
        await self.outer.canvas_removed(instance_id, chat=self._redirect(chat, "canvas_removed"))

    async def open_terminal(
        self,
        title: str,
        *,
        chat: str | None = None,
        cwd: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ProviderTerminal:
        return await self.outer.open_terminal(
            title,
            chat=self._redirect(chat, "open_terminal"),
            cwd=cwd,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )

    async def open_tool_chat(
        self,
        title: str,
        *,
        tool_call_id: str,
        chat: str | None = None,
        interactivity: str = "read-only",
    ) -> ProviderChat:
        opened = await self.outer.open_tool_chat(
            title,
            tool_call_id=tool_call_id,
            chat=self._redirect(chat, "open_tool_chat"),
            interactivity=interactivity,
        )
        self._owned.add(opened.resource)
        return opened

    async def external_turn(
        self,
        text: str,
        run: Callable[[TurnSink], Awaitable[None]],
        *,
        chat: str | None = None,
    ) -> bool:
        target = self._own(chat, "external_turn")
        if target is None:
            return False
        return await self.outer.external_turn(text, run, chat=target)


def _like(original: Sequence[Any], items: Iterable[Any]) -> Any:
    """*items* as the kind of sequence *original* is, so an unchanged copy compares equal."""
    return tuple(items) if isinstance(original, tuple) else list(items)


class MaskingSink:
    """Wraps a real `ActionTurnSink` handed to `StructuredTurn`: masks what it publishes.

    The structured turn publishes the `StructuredOutput` call's input and its accepted result
    (`structuredContent`) itself, on the sink it is given. Validation still runs on the unmasked
    arguments in the turn, and the tool result handed back to the agent is untouched; only the
    published copies are masked. *before* runs ahead of each publish (the tee's line buffer).
    """

    def __init__(
        self,
        sink: Any,
        redactor: Redactor,
        before: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._sink = sink
        self._redactor = redactor
        self._before = before

    @property
    def on_text(self) -> Callable[[str], None] | None:
        return self._sink.on_text  # type: ignore[no-any-return]

    @on_text.setter
    def on_text(self, value: Callable[[str], None] | None) -> None:
        self._sink.on_text = value

    async def _flush(self) -> None:
        if self._before is not None:
            await self._before()

    def _str(self, value: str | None) -> str | None:
        return self._redactor.mask(value) if value is not None else None

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
        await self._flush()
        await self._sink.tool_call_started(
            call_id,
            name,
            self._redactor.mask_value(tool_input),
            display_name=self._str(display_name),
            intention=self._str(intention),
            meta=meta,
        )

    async def tool_call_completed(
        self,
        call_id: str,
        result: Any = None,
        *,
        success: bool = True,
        past_tense_message: str | None = None,
    ) -> None:
        await self._flush()
        await self._sink.tool_call_completed(
            call_id,
            self._redactor.mask_value(result),
            success=success,
            past_tense_message=self._str(past_tense_message),
        )

    async def system_notification(
        self, text: str, *, markdown: bool = False, meta: Mapping[str, Any] | None = None
    ) -> None:
        await self._flush()
        await self._sink.system_notification(self._str(text) or "", markdown=markdown, meta=meta)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sink, name)


class TurnTee:
    """Wraps a `TurnSink`: records the text and usage `agent()` needs, forwards the rest.

    With an enabled *redactor* everything forwarded to the sink is masked (text a
    line at a time, so a secret split across deltas is still caught) while `text`
    and `reply` keep the raw text for the engine to mask itself. Nothing handed
    back to the inner session changes. *on_waiting* hears `True` when the first
    confirmation or input request opens and `False` when the last one closes.
    """

    def __init__(
        self,
        sink: TurnSink,
        *,
        redactor: Redactor | None = None,
        on_waiting: Callable[[bool], Awaitable[None]] | None = None,
    ) -> None:
        self.sink = sink
        self.text: list[str] = []
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.failed: bool = False
        self.failure: str | None = None
        self._redactor = redactor if redactor is not None and redactor.enabled else None
        self._text_stream = self._redactor.stream() if self._redactor else None
        self._reasoning_stream = self._redactor.stream() if self._redactor else None
        self._on_waiting = on_waiting
        self._open = 0

    @property
    def reply(self) -> str:
        return "".join(self.text)

    def _mask(self, value: Any) -> Any:
        return self._redactor.mask_value(value) if self._redactor else value

    def _mask_str(self, value: str | None) -> str | None:
        return self._redactor.mask(value) if self._redactor and value is not None else value

    async def flush(self) -> None:
        """Forward what the line buffers still hold (a no-op with masking off)."""
        if self._text_stream is not None and (out := self._text_stream.flush()):
            await self.sink.text_delta(out)
        if self._reasoning_stream is not None and (out := self._reasoning_stream.flush()):
            await self.sink.reasoning_delta(out)

    async def text_delta(self, text: str) -> None:
        self.text.append(text)
        if self._text_stream is None:
            await self.sink.text_delta(text)
            return
        if self._reasoning_stream is not None and (out := self._reasoning_stream.flush()):
            await self.sink.reasoning_delta(out)
        if out := self._text_stream.feed(text):
            await self.sink.text_delta(out)

    async def reasoning_delta(self, text: str) -> None:
        if self._reasoning_stream is None or self._text_stream is None:
            await self.sink.reasoning_delta(text)
            return
        if out := self._text_stream.flush():
            await self.sink.text_delta(out)
        if out := self._reasoning_stream.feed(text):
            await self.sink.reasoning_delta(out)

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
        await self.flush()
        await self.sink.tool_call_started(
            call_id,
            name,
            self._mask(tool_input),
            display_name=self._mask_str(display_name),
            intention=self._mask_str(intention),
            meta=meta,
        )

    async def tool_call_delta(
        self,
        call_id: str,
        content: str | None = None,
        *,
        invocation_message: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        await self.flush()
        await self.sink.tool_call_delta(
            call_id,
            self._mask_str(content),
            invocation_message=self._mask_str(invocation_message),
            meta=meta,
        )

    async def tool_call_output(
        self,
        call_id: str,
        content: Sequence[Mapping[str, Any]],
        *,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        await self.flush()
        await self.sink.tool_call_output(call_id, self._mask(content), meta=meta)

    async def tool_call_completed(
        self,
        call_id: str,
        result: Any = None,
        *,
        success: bool = True,
        past_tense_message: str | None = None,
    ) -> None:
        await self.flush()
        await self.sink.tool_call_completed(
            call_id,
            self._mask(result),
            success=success,
            past_tense_message=self._mask_str(past_tense_message),
        )

    async def system_notification(
        self, text: str, *, markdown: bool = False, meta: Mapping[str, Any] | None = None
    ) -> None:
        await self.flush()
        await self.sink.system_notification(
            self._mask_str(text) or "", markdown=markdown, meta=meta
        )

    async def usage(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        model: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        await self.flush()
        if input_tokens is not None:
            self.input_tokens += input_tokens
        if output_tokens is not None:
            self.output_tokens += output_tokens
        await self.sink.usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            model=model,
            meta=meta,
        )

    async def turn_failed(
        self,
        message: str,
        error_type: str = "agent.turn",
        duration_ms: int = 0,
        *,
        resumable: bool = False,
    ) -> None:
        await self.flush()
        self.failed = True
        self.failure = self._mask_str(message)
        await self.sink.turn_failed(
            self.failure or "", error_type, duration_ms, resumable=resumable
        )

    async def _waiting(self, delta: int) -> None:
        self._open += delta
        if self._on_waiting is not None and self._open == (1 if delta > 0 else 0):
            await self._on_waiting(delta > 0)

    def _masked_bytes(self, data: bytes | None) -> bytes | None:
        if data is None or self._redactor is None:
            return data
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        masked = self._redactor.mask(text)
        return data if masked == text else masked.encode("utf-8")

    def _masked_confirmation(self, call: ToolConfirmation) -> ToolConfirmation:
        """The copy of *call* that is published; the agent's own object is untouched."""
        if self._redactor is None:
            return call
        masked = dataclasses.replace(
            call,
            invocation_message=self._mask_str(call.invocation_message) or "",
            display_name=self._mask_str(call.display_name),
            tool_input=self._mask(call.tool_input),
            confirmation_title=self._mask_str(call.confirmation_title),
            options=_like(
                call.options,
                (dataclasses.replace(o, label=self._mask_str(o.label) or "") for o in call.options),
            ),
            edits=_like(
                call.edits,
                (
                    dataclasses.replace(
                        e,
                        uri=self._mask_str(e.uri) or "",
                        before=self._masked_bytes(e.before),
                        after=self._masked_bytes(e.after),
                        renamed_to=self._mask_str(e.renamed_to),
                    )
                    for e in call.edits
                ),
            ),
        )
        return call if masked == call else masked

    def _masked_request(self, request: InputRequest) -> InputRequest:
        """The copy of *request* that is published; the agent's own object is untouched."""
        if self._redactor is None:
            return request
        masked = dataclasses.replace(
            request,
            message=self._mask_str(request.message),
            url=self._mask_str(request.url),
            questions=_like(
                request.questions,
                (
                    dataclasses.replace(
                        q,
                        message=self._mask_str(q.message) or "",
                        options=_like(q.options, self._mask(list(q.options))),
                        extra=self._mask(dict(q.extra)),
                    )
                    for q in request.questions
                ),
            ),
        )
        return request if masked == request else masked

    async def request_input(self, request: InputRequest) -> InputOutcome:
        await self.flush()
        await self._waiting(1)
        try:
            return await self.sink.request_input(self._masked_request(request))
        finally:
            await self._waiting(-1)

    async def confirm_tool_call(self, call: ToolConfirmation) -> ToolConfirmationOutcome:
        await self.flush()
        await self._waiting(1)
        try:
            return await self.sink.confirm_tool_call(self._masked_confirmation(call))
        finally:
            await self._waiting(-1)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.sink, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        async def forward(*args: Any, **kwargs: Any) -> Any:
            await self.flush()
            return await attr(*args, **kwargs)

        return forward
