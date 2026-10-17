"""Spike: an inner session's turn runs inside a worker chat of an outer session.

Proves plan section 2 (in-process dispatch) and the "Sink bridging" risk of
section 9 over a real in-memory `Host`. The bridge classes live here, not in
`src`; M3 promotes them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

from ahp_host import (
    AgentInfo,
    AgentSessionContext,
    Host,
    LoopbackSingleUserPolicy,
    TurnSink,
    UserMessage,
)
from ahp_host.provider.base import (
    AgentSession,
    BackgroundWork,
    Canvas,
    ProviderChat,
    ProviderTerminal,
    SessionPublisher,
)
from ahp_host.provider.echo import EchoProvider

from ..hosting import connect, found, open_session, run_turn, shut, state, text_of  # noqa: TID252

SESSION = "flow-spike:/run-1"


class ScopedPublisher:
    """An outer `SessionPublisher` for an inner session whose world is one worker chat."""

    def __init__(self, outer: SessionPublisher, chat: str) -> None:
        self.outer = outer
        self.chat = chat
        #: Session-wide calls the inner session made; an inner session must not
        #: rename or reconfigure the run, so they stop here.
        self.dropped: list[str] = []

    async def customizations_changed(
        self,
        customizations: Sequence[Mapping[str, Any]],
        server_tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.dropped.append("customizations_changed")

    async def activity_changed(self, activity: str | None) -> None:
        self.dropped.append("activity_changed")

    async def changes_published(
        self, changeset: Any, changes: Sequence[Any], *, chat: str | None = None
    ) -> str:
        return await self.outer.changes_published(
            changeset, changes, chat=chat if chat is not None else self.chat
        )

    async def mcp_server_changed(
        self, customization_id: str, state: Mapping[str, Any], channel: str | None = None
    ) -> None:
        self.dropped.append("mcp_server_changed")

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.dropped.append("progress")

    async def title_changed(self, title: str) -> None:
        self.dropped.append("title_changed")

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        self.dropped.append("config_changed")

    async def background_work_set(self, work: BackgroundWork, *, chat: str | None = None) -> None:
        await self.outer.background_work_set(work, chat=chat if chat is not None else self.chat)

    async def background_work_removed(self, work_id: str, *, chat: str | None = None) -> None:
        await self.outer.background_work_removed(
            work_id, chat=chat if chat is not None else self.chat
        )

    async def canvas_set(self, canvas: Canvas, *, chat: str | None = None) -> str:
        return await self.outer.canvas_set(canvas, chat=chat if chat is not None else self.chat)

    async def canvas_removed(self, instance_id: str, *, chat: str | None = None) -> None:
        await self.outer.canvas_removed(instance_id, chat=chat if chat is not None else self.chat)

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
            chat=chat if chat is not None else self.chat,
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
        return await self.outer.open_tool_chat(
            title,
            tool_call_id=tool_call_id,
            chat=chat if chat is not None else self.chat,
            interactivity=interactivity,
        )

    async def external_turn(
        self,
        text: str,
        run: Callable[[TurnSink], Awaitable[None]],
        *,
        chat: str | None = None,
    ) -> bool:
        return await self.outer.external_turn(
            text, run, chat=chat if chat is not None else self.chat
        )


class TextTee:
    """Records every `text_delta` it forwards: how the dispatcher gets agent()'s text."""

    def __init__(self, sink: TurnSink) -> None:
        self.sink = sink
        self.text: list[str] = []

    async def text_delta(self, text: str) -> None:
        self.text.append(text)
        await self.sink.text_delta(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.sink, name)


class SpikeSession:
    def __init__(self, provider: SpikeProvider, context: AgentSessionContext) -> None:
        self.provider = provider
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        provider = self.provider
        context = self.context
        call_id = "agent-1"
        await sink.tool_call_started(
            call_id, "agent", {"prompt": message.text}, display_name="agent()"
        )
        publisher = context.publisher
        assert publisher is not None
        worker = await publisher.open_tool_chat(
            f"agent: {message.text}", tool_call_id=call_id, chat=message.chat_uri
        )
        scoped = ScopedPublisher(publisher, worker.resource)
        provider.scoped.append(scoped)
        inner = await provider.inner.create_session(
            AgentSessionContext(
                session_uri=context.session_uri,
                chat_uri=worker.resource,
                provider_id="echo",
                publisher=scoped,
            )
        )
        done = asyncio.Event()
        tee: TextTee | None = None

        async def run(inner_sink: TurnSink) -> None:
            nonlocal tee
            tee = TextTee(inner_sink)
            try:
                await inner.send_user_message(
                    UserMessage(text=message.text, chat_uri=worker.resource),
                    cast(TurnSink, tee),
                )
            finally:
                done.set()

        # `run_turn` returns once the turn has STARTED, so wait on our own signal.
        assert await worker.run_turn(message.text, run)
        await done.wait()
        await inner.aclose()
        assert tee is not None
        reply = "".join(tee.text)
        await sink.tool_call_completed(
            call_id,
            {
                "content": [
                    {
                        "type": "subagent",
                        "resource": worker.resource,
                        "title": f"agent: {message.text}",
                    }
                ]
            },
            past_tense_message="agent() returned",
        )
        await sink.text_delta(f"agent() returned: {reply}")

    async def cancel(self, reason: str | None = None) -> None:
        return None

    async def aclose(self) -> None:
        return None


class SpikeProvider:
    agent = AgentInfo(
        provider="flow-spike",
        display_name="Flow spike",
        description="Runs each message as one agent() call in a worker chat.",
    )

    def __init__(self) -> None:
        self.inner = EchoProvider()
        self.scoped: list[ScopedPublisher] = []

    async def create_session(self, context: AgentSessionContext) -> AgentSession:
        return SpikeSession(self, context)


async def test_spike_worker_chat_runs_inner_turn() -> None:
    host = Host(SpikeProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, SESSION, "flow-spike")
        outer_turn = await run_turn(wire, default, "t1", "hi")
        assert text_of(outer_turn) == "agent() returned: You said: hi"

        workers = [
            chat
            for chat in state(host, SESSION)["chats"]
            if (chat.get("origin") or {}).get("kind") == "tool"
        ]
        assert len(workers) == 1
        assert workers[0]["origin"] == {"kind": "tool", "chat": default, "toolCallId": "agent-1"}

        (subagent,) = found(outer_turn, lambda m: m.get("type") == "subagent")
        worker = str(subagent["resource"])

        def finished() -> bool:
            chat = state(host, worker)
            return len(chat.get("turns", [])) == 1 and chat.get("activeTurn") is None

        assert await wire.until(finished), state(host, worker)
        (turn,) = state(host, worker)["turns"]
        assert turn["message"] == {"text": "hi", "origin": {"kind": "agent"}}
        assert text_of(turn) == "You said: hi"
        # The host omits `activeTurn` from chat state once the turn has ended
        # (measured: a plain subscript raised KeyError), so absent means None.
        assert state(host, worker).get("activeTurn") is None
    finally:
        await shut(host, wire, serving)


async def test_spike_worker_chat_publisher_is_scoped() -> None:
    provider = SpikeProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, SESSION, "flow-spike")
        outer_turn = await run_turn(wire, default, "t1", "hi")
        (subagent,) = found(outer_turn, lambda m: m.get("type") == "subagent")
        worker = str(subagent["resource"])
        scoped = provider.scoped[0]

        await scoped.background_work_set(
            BackgroundWork(id="inner-1", kind="shell", label="inner shell", command="echo hi")
        )

        def listed() -> bool:
            return [w["id"] for w in state(host, worker).get("backgroundWork", [])] == ["inner-1"]

        assert await wire.until(listed), state(host, worker)
        assert [w["id"] for w in state(host, worker)["backgroundWork"]] == ["inner-1"]
        assert not state(host, default).get("backgroundWork")

        await scoped.title_changed("hijacked")
        assert state(host, SESSION).get("title") != "hijacked"
        assert scoped.dropped == ["title_changed"]
    finally:
        await shut(host, wire, serving)
