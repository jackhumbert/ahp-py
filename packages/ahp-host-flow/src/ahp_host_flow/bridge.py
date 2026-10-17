"""Bridges an inner provider session into a worker chat (plan section 8, M0 spike notes)."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final

from ahp_host import TurnSink
from ahp_host.provider.base import (
    BackgroundWork,
    Canvas,
    ProviderChat,
    ProviderTerminal,
    SessionPublisher,
)

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


class TurnTee:
    """Wraps a `TurnSink`: records the text and usage `agent()` needs, forwards the rest."""

    def __init__(self, sink: TurnSink) -> None:
        self.sink = sink
        self.text: list[str] = []
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.failed: bool = False
        self.failure: str | None = None

    @property
    def reply(self) -> str:
        return "".join(self.text)

    async def text_delta(self, text: str) -> None:
        self.text.append(text)
        await self.sink.text_delta(text)

    async def usage(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        model: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
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
        self.failed = True
        self.failure = message
        await self.sink.turn_failed(message, error_type, duration_ms, resumable=resumable)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.sink, name)
