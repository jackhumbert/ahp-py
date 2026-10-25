"""Bridges an inner provider session into a worker chat (plan section 8, M0 spike notes)."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from ahp_host import TurnSink
from ahp_host.provider.base import (
    BackgroundWork,
    Canvas,
    ClientToolCall,
    InputOutcome,
    InputRequest,
    OriginTurn,
    ProviderChat,
    ProviderTerminal,
    ResolvesOriginTurn,
    SessionPublisher,
    ToolConfirmation,
    ToolConfirmationOutcome,
    ToolResult,
)

from ahp_host_flow.engine import Redactor
from ahp_host_flow.engine.usage import has_cost_usd, read_usage

if TYPE_CHECKING:
    from ahp_host_flow.approvals import Approver

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

    def origin_turn(self, chat: str | None = None) -> OriginTurn | None:
        """The turn the worker chat works for, so an inner provider can find its caller's turn.

        Only the worker chat and the tool chats opened here are answered; anything else is
        foreign and gets ``None``, like every other call that names a foreign chat.
        """
        target = self._own(chat, "origin_turn")
        resolver = self.outer
        if target is None or not isinstance(resolver, ResolvesOriginTurn):
            return None
        return resolver.origin_turn(target)

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


class Watchdog:
    """Calls *on_expire* once when *seconds* pass without a `touch`.

    One task sleeps until `last touch + seconds`, re-sleeping after each touch. While paused it
    never fires; `resume` counts as a touch, so the clock restarts when the pause ends.
    """

    def __init__(self, seconds: float, on_expire: Callable[[], None]) -> None:
        self.seconds = seconds
        self._on_expire = on_expire
        self._last = time.monotonic()
        self._running = asyncio.Event()
        self._running.set()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._last = time.monotonic()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._watch())

    def touch(self) -> None:
        self._last = time.monotonic()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self.touch()
        self._running.set()

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _watch(self) -> None:
        while True:
            await self._running.wait()
            remaining = self._last + self.seconds - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            self._on_expire()
            return


class TurnTee:
    """Wraps a `TurnSink`: records the text and usage `agent()` needs, forwards the rest.

    With an enabled *redactor* everything forwarded to the sink is masked (text a
    line at a time, so a secret split across deltas is still caught) while `text`
    and `reply` keep the raw text for the engine to mask itself. Nothing handed
    back to the inner session changes. *on_waiting* hears `True` when the first
    confirmation or input request opens and `False` when the last one closes. An *approver*
    answers a confirmation itself when the policy or a rule decides it, on the agent's unmasked
    call and without publishing anything; otherwise the confirmation goes to the sink. Every
    event forwarded touches the *watchdog*; an open confirmation or input request pauses it, and
    so does a client tool call while the client runs it.
    """

    def __init__(
        self,
        sink: TurnSink,
        *,
        redactor: Redactor | None = None,
        on_waiting: Callable[[bool], Awaitable[None]] | None = None,
        approver: Approver | None = None,
        on_usage: Callable[[Mapping[str, Any]], None] | None = None,
        watchdog: Watchdog | None = None,
        cost_base: float = 0.0,
    ) -> None:
        self.sink = sink
        self.watchdog = watchdog
        self.text: list[str] = []
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cache_read_tokens: int = 0
        self.cost_usd: float | None = None
        #: ACP's running session cost when the call began (a reused session's earlier calls).
        self.cost_base = cost_base
        #: The last running session total ACP reported, for the next call on the same session.
        self.session_cost: float | None = None
        self.failed: bool = False
        self.failure: str | None = None
        self.error_type: str | None = None
        self.resumable: bool = False
        self._redactor = redactor if redactor is not None and redactor.enabled else None
        self._text_stream = self._redactor.stream() if self._redactor else None
        self._reasoning_stream = self._redactor.stream() if self._redactor else None
        self._on_waiting = on_waiting
        self._approver = approver
        self._on_usage = on_usage
        # Request-scoped readings not yet covered by a turn report: (in, out, cache read).
        self._pending = (0, 0, 0)
        self._open = 0
        #: What holds the watchdog paused: open prompts and client tool calls in flight.
        self._held = 0

    @property
    def reply(self) -> str:
        return "".join(self.text)

    def totals(self) -> dict[str, Any]:
        """What the call has used so far: the turn reports plus the requests not yet reported."""
        in_pending, out_pending, cache_pending = self._pending
        return {
            "input_tokens": self.input_tokens + in_pending,
            "output_tokens": self.output_tokens + out_pending,
            "cache_read_tokens": self.cache_read_tokens + cache_pending,
            "cost_usd": self.cost_usd or 0.0,
        }

    def _touch(self) -> None:
        if self.watchdog is not None:
            self.watchdog.touch()

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
        self._touch()
        self.text.append(text)
        if self._text_stream is None:
            await self.sink.text_delta(text)
            return
        if self._reasoning_stream is not None and (out := self._reasoning_stream.flush()):
            await self.sink.reasoning_delta(out)
        if out := self._text_stream.feed(text):
            await self.sink.text_delta(out)

    async def reasoning_delta(self, text: str) -> None:
        self._touch()
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
        self._touch()
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
        self._touch()
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
        self._touch()
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
        self._touch()
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
        self._touch()
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
        self._touch()
        await self.flush()
        reading = read_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            meta=meta,
        )
        if reading.request_scoped:
            in_pending, out_pending, cache_pending = self._pending
            self._pending = (
                in_pending + reading.input_tokens,
                out_pending + reading.output_tokens,
                cache_pending + reading.cache_read_tokens,
            )
        else:
            # The turn report covers the requests seen so far.
            self._pending = (0, 0, 0)
            self.input_tokens += reading.input_tokens
            self.output_tokens += reading.output_tokens
            self.cache_read_tokens += reading.cache_read_tokens
            if reading.cost_usd is not None:
                if has_cost_usd(meta):
                    # Claude's per-turn cost: turns add up.
                    self.cost_usd = (self.cost_usd or 0.0) + reading.cost_usd
                else:
                    # ACP's cost is the session's running total: this call's share is what it
                    # added since the call began.
                    self.session_cost = reading.cost_usd
                    self.cost_usd = max(0.0, reading.cost_usd - self.cost_base)
        await self.sink.usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            model=model,
            meta=meta,
        )
        if self._on_usage is not None:
            self._on_usage(self.totals())

    async def turn_failed(
        self,
        message: str,
        error_type: str = "agent.turn",
        duration_ms: int = 0,
        *,
        resumable: bool = False,
    ) -> None:
        self._touch()
        await self.flush()
        self.failed = True
        self.failure = self._mask_str(message)
        self.error_type = error_type
        self.resumable = resumable
        await self.sink.turn_failed(
            self.failure or "", error_type, duration_ms, resumable=resumable
        )

    def _hold(self, delta: int) -> None:
        """Pause the watchdog while anything waits on someone else; resume when nothing does."""
        self._held += delta
        if self.watchdog is not None:
            if delta > 0 and self._held == 1:
                self.watchdog.pause()
            elif delta < 0 and self._held == 0:
                self.watchdog.resume()

    async def _waiting(self, delta: int) -> None:
        self._open += delta
        self._hold(delta)
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
        if self._approver is not None and (decided := self._approver.decide(call)) is not None:
            return decided
        await self._waiting(1)
        try:
            return await self.sink.confirm_tool_call(self._masked_confirmation(call))
        finally:
            await self._waiting(-1)

    async def run_client_tool(self, call: ClientToolCall) -> ToolResult:
        self._touch()
        await self.flush()
        # The client runs it in its own time (a build, a search): not the inner agent idling.
        self._hold(1)
        try:
            return await self.sink.run_client_tool(call)
        finally:
            self._hold(-1)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.sink, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        async def forward(*args: Any, **kwargs: Any) -> Any:
            self._touch()
            await self.flush()
            return await attr(*args, **kwargs)

        return forward
