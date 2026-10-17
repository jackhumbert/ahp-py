"""ScopedPublisher and TurnTee, without a host."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest
from ahp_host import TurnSink
from ahp_host.provider.base import (
    BackgroundWork,
    Canvas,
    InputRequest,
    SessionPublisher,
    ToolConfirmation,
)

from ahp_host_flow.bridge import DROPPED_KEEP, ScopedPublisher, TurnTee
from ahp_host_flow.engine import Redactor

WORKER = "chat:/worker"

SCOPED_METHODS = [
    "changes_published",
    "background_work_set",
    "background_work_removed",
    "canvas_set",
    "canvas_removed",
    "open_terminal",
    "open_tool_chat",
    "external_turn",
]


class RecordingPublisher:
    """Every `SessionPublisher` method, recording `(name, chat kwarg or None)`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.next_tool_chat = "chat:/tool"

    async def customizations_changed(
        self,
        customizations: Sequence[Mapping[str, Any]],
        server_tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.calls.append(("customizations_changed", None))

    async def activity_changed(self, activity: str | None) -> None:
        self.calls.append(("activity_changed", None))

    async def changes_published(
        self, changeset: Any, changes: Sequence[Any], *, chat: str | None = None
    ) -> str:
        self.calls.append(("changes_published", chat))
        return "cs:/1"

    async def mcp_server_changed(
        self, customization_id: str, state: Mapping[str, Any], channel: str | None = None
    ) -> None:
        self.calls.append(("mcp_server_changed", None))

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.calls.append(("progress", None))

    async def title_changed(self, title: str) -> None:
        self.calls.append(("title_changed", None))

    async def config_changed(self, values: Mapping[str, Any], *, chat: str | None = None) -> None:
        self.calls.append(("config_changed", chat))

    async def background_work_set(self, work: BackgroundWork, *, chat: str | None = None) -> None:
        self.calls.append(("background_work_set", chat))

    async def background_work_removed(self, work_id: str, *, chat: str | None = None) -> None:
        self.calls.append(("background_work_removed", chat))

    async def canvas_set(self, canvas: Canvas, *, chat: str | None = None) -> str:
        self.calls.append(("canvas_set", chat))
        return "canvas:/1"

    async def canvas_removed(self, instance_id: str, *, chat: str | None = None) -> None:
        self.calls.append(("canvas_removed", chat))

    async def open_terminal(
        self,
        title: str,
        *,
        chat: str | None = None,
        cwd: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> Any:
        self.calls.append(("open_terminal", chat))
        return SimpleNamespace(resource="terminal:/1")

    async def open_tool_chat(
        self,
        title: str,
        *,
        tool_call_id: str,
        chat: str | None = None,
        interactivity: str = "read-only",
    ) -> Any:
        self.calls.append(("open_tool_chat", chat))
        return SimpleNamespace(resource=self.next_tool_chat)

    async def external_turn(
        self,
        text: str,
        run: Callable[[TurnSink], Awaitable[None]],
        *,
        chat: str | None = None,
    ) -> bool:
        self.calls.append(("external_turn", chat))
        return True


def make() -> tuple[RecordingPublisher, ScopedPublisher]:
    outer = RecordingPublisher()
    return outer, ScopedPublisher(cast(SessionPublisher, outer), WORKER)


async def _noop(_sink: TurnSink) -> None:
    return None


def _scoped_calls(scoped: ScopedPublisher, chat: str | None) -> list[Awaitable[Any]]:
    kw: dict[str, Any] = {} if chat is None else {"chat": chat}
    work = BackgroundWork(id="w", kind="shell", label="l", command="c")
    return [
        scoped.changes_published(None, [], **kw),
        scoped.background_work_set(work, **kw),
        scoped.background_work_removed("w", **kw),
        scoped.canvas_set(cast(Canvas, None), **kw),
        scoped.canvas_removed("c", **kw),
        scoped.open_terminal("t", **kw),
        scoped.open_tool_chat("t", tool_call_id="call-1", **kw),
        scoped.external_turn("x", _noop, **kw),
    ]


async def test_chat_scoped_calls_default_to_the_worker_chat() -> None:
    outer, scoped = make()
    for call in _scoped_calls(scoped, None):
        await call
    assert outer.calls == [(name, WORKER) for name in SCOPED_METHODS]


MAIN = "chat:/main"
SIBLING = "chat:/sibling-worker"


async def test_explicit_worker_chat_is_honoured() -> None:
    outer, scoped = make()
    for call in _scoped_calls(scoped, WORKER):
        await call
    assert outer.calls == [(name, WORKER) for name in SCOPED_METHODS]


@pytest.mark.parametrize("foreign", [MAIN, SIBLING])
async def test_foreign_chat_never_reaches_the_outer_publisher(foreign: str) -> None:
    outer, scoped = make()
    await scoped.config_changed({"permissionMode": "auto"}, chat=foreign)
    assert await scoped.changes_published(None, [], chat=foreign) == ""
    assert await scoped.external_turn("x", _noop, chat=foreign) is False
    work = BackgroundWork(id="w", kind="shell", label="l", command="c")
    await scoped.background_work_set(work, chat=foreign)
    await scoped.background_work_removed("w", chat=foreign)
    await scoped.canvas_set(cast(Canvas, None), chat=foreign)
    await scoped.canvas_removed("c", chat=foreign)
    await scoped.open_terminal("t", chat=foreign)
    await scoped.open_tool_chat("t", tool_call_id="call-1", chat=foreign)
    # Config, changes and turns are dropped; things the inner session makes
    # are redirected to its own worker chat. Nothing is aimed at `foreign`.
    assert [chat for _, chat in outer.calls] == [WORKER] * 6
    assert [name for name, _ in outer.calls] == [
        "background_work_set",
        "background_work_removed",
        "canvas_set",
        "canvas_removed",
        "open_terminal",
        "open_tool_chat",
    ]
    assert scoped.dropped_counts["foreign_chat:config_changed"] == 1
    assert scoped.dropped_counts["foreign_chat:external_turn"] == 1
    assert scoped.dropped_counts["foreign_chat:changes_published"] == 1


async def test_a_tool_chat_the_inner_session_opened_stays_addressable() -> None:
    outer, scoped = make()
    opened = await scoped.open_tool_chat("t", tool_call_id="call-1")
    await scoped.config_changed({"effort": "low"}, chat=opened.resource)
    assert await scoped.external_turn("x", _noop, chat=opened.resource) is True
    # ... and it can open one more beneath it, which is then addressable too.
    outer.next_tool_chat = "chat:/tool-2"
    nested = await scoped.open_tool_chat("t2", tool_call_id="call-2", chat=opened.resource)
    await scoped.config_changed({"effort": "high"}, chat=nested.resource)
    assert outer.calls == [
        ("open_tool_chat", WORKER),
        ("config_changed", "chat:/tool"),
        ("external_turn", "chat:/tool"),
        ("open_tool_chat", "chat:/tool"),
        ("config_changed", "chat:/tool-2"),
    ]
    assert not scoped.dropped


async def test_dropped_is_bounded() -> None:
    _, scoped = make()
    for _ in range(DROPPED_KEEP * 4):
        await scoped.progress(0.5)
    assert len(scoped.dropped) == DROPPED_KEEP
    assert scoped.dropped_counts["progress"] == DROPPED_KEEP * 4


async def test_config_changed_lands_on_the_worker_chat() -> None:
    outer, scoped = make()
    await scoped.config_changed({"effort": "high"})
    assert outer.calls == [("config_changed", WORKER)]


async def test_session_wide_calls_are_dropped_in_order() -> None:
    outer, scoped = make()
    await scoped.title_changed("t")
    await scoped.customizations_changed([])
    await scoped.activity_changed("a")
    await scoped.mcp_server_changed("m", {})
    await scoped.progress(0.5)
    assert outer.calls == []
    assert list(scoped.dropped) == [
        "title_changed",
        "customizations_changed",
        "activity_changed",
        "mcp_server_changed",
        "progress",
    ]


class RecordingSink:
    turn_id = "turn-1"

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.outcome: Any = object()
        self.gate: asyncio.Event | None = None

    async def text_delta(self, text: str) -> None:
        self.events.append(("text_delta", text))

    async def usage(self, **kwargs: Any) -> None:
        self.events.append(("usage", kwargs))

    async def turn_failed(
        self,
        message: str,
        error_type: str = "agent.turn",
        duration_ms: int = 0,
        *,
        resumable: bool = False,
    ) -> None:
        self.events.append(("turn_failed", message, error_type, duration_ms, resumable))

    async def reasoning_delta(self, text: str) -> None:
        self.events.append(("reasoning_delta", text))

    async def tool_call_started(
        self, call_id: str, name: str, tool_input: Any = None, **kwargs: Any
    ) -> None:
        self.events.append(("tool_call_started", call_id, name, tool_input, kwargs))

    async def tool_call_output(self, call_id: str, content: Any, **kwargs: Any) -> None:
        self.events.append(("tool_call_output", call_id, content, kwargs))

    async def tool_call_completed(self, call_id: str, result: Any = None, **kwargs: Any) -> None:
        self.events.append(("tool_call_completed", call_id, result, kwargs))

    async def system_notification(self, text: str, **kwargs: Any) -> None:
        self.events.append(("system_notification", text, kwargs))

    async def request_input(self, request: Any) -> Any:
        self.events.append(("request_input", request))
        return self.outcome

    async def confirm_tool_call(self, call: Any) -> Any:
        self.events.append(("confirm_tool_call", call))
        if self.gate is not None:
            await self.gate.wait()
        return self.outcome


def _usage(
    input_tokens: int | None, output_tokens: int | None, model: str | None = None
) -> tuple[str, dict[str, Any]]:
    return (
        "usage",
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": None,
            "model": model,
            "meta": None,
        },
    )


async def test_turn_tee_records_text_and_sums_usage() -> None:
    sink = RecordingSink()
    tee = TurnTee(cast(TurnSink, sink))
    await tee.text_delta("a")
    await tee.text_delta("b")
    await tee.usage(input_tokens=1, output_tokens=3)
    await tee.usage(input_tokens=2, output_tokens=4, model="m")
    assert tee.text == ["a", "b"]
    assert tee.reply == "ab"
    assert tee.input_tokens == 3
    assert tee.output_tokens == 7
    assert tee.failed is False
    assert tee.failure is None
    assert sink.events == [
        ("text_delta", "a"),
        ("text_delta", "b"),
        _usage(1, 3),
        _usage(2, 4, "m"),
    ]
    assert tee.turn_id == "turn-1"


async def test_turn_tee_marks_failure_and_forwards() -> None:
    sink = RecordingSink()
    tee = TurnTee(cast(TurnSink, sink))
    await tee.turn_failed("boom")
    assert tee.failed is True
    assert tee.failure == "boom"
    assert sink.events == [("turn_failed", "boom", "agent.turn", 0, False)]


SECRET = "AKIAABCDEFGHIJKLMNOP"
MASKED = "[redacted]"


def _masking_tee(
    sink: RecordingSink, on_waiting: Callable[[bool], Awaitable[None]] | None = None
) -> TurnTee:
    return TurnTee(cast(TurnSink, sink), redactor=Redactor(), on_waiting=on_waiting)


async def test_turn_tee_masking_off_keeps_todays_events() -> None:
    sink = RecordingSink()
    tee = TurnTee(cast(TurnSink, sink), redactor=Redactor(builtin=False))
    await tee.text_delta(f"key {SECRET}")
    await tee.tool_call_started("c1", "t", {"k": SECRET})
    assert sink.events == [
        ("text_delta", f"key {SECRET}"),
        (
            "tool_call_started",
            "c1",
            "t",
            {"k": SECRET},
            {"display_name": None, "intention": None, "meta": None},
        ),
    ]


async def test_turn_tee_masks_a_secret_split_across_deltas() -> None:
    sink = RecordingSink()
    tee = _masking_tee(sink)
    await tee.text_delta("key AKIAABCDE")
    await tee.text_delta("FGHIJKLMNOP\nnext")
    assert sink.events == [("text_delta", f"key {MASKED}\n")]
    assert tee.reply == f"key {SECRET}\nnext"
    await tee.flush()
    assert sink.events[-1] == ("text_delta", "next")
    assert SECRET not in repr(sink.events)


async def test_turn_tee_holds_unterminated_text_until_a_tool_call() -> None:
    sink = RecordingSink()
    tee = _masking_tee(sink)
    await tee.text_delta("partial")
    assert sink.events == []
    await tee.tool_call_started("c1", "t")
    assert sink.events == [
        ("text_delta", "partial"),
        (
            "tool_call_started",
            "c1",
            "t",
            None,
            {"display_name": None, "intention": None, "meta": None},
        ),
    ]


async def test_turn_tee_masks_tool_traffic_notifications_and_failure() -> None:
    sink = RecordingSink()
    tee = _masking_tee(sink)
    await tee.reasoning_delta(f"think {SECRET}\n")
    await tee.tool_call_started("c1", "t", {"k": SECRET}, display_name=SECRET, meta={"m": 1})
    await tee.tool_call_output("c1", [{"text": SECRET}])
    await tee.tool_call_completed("c1", {"content": [SECRET]}, past_tense_message=SECRET)
    await tee.system_notification(f"note {SECRET}")
    await tee.turn_failed(f"bad {SECRET}")
    assert tee.failure == f"bad {MASKED}"
    assert sink.events == [
        ("reasoning_delta", f"think {MASKED}\n"),
        (
            "tool_call_started",
            "c1",
            "t",
            {"k": MASKED},
            {"display_name": MASKED, "intention": None, "meta": {"m": 1}},
        ),
        ("tool_call_output", "c1", [{"text": MASKED}], {"meta": None}),
        (
            "tool_call_completed",
            "c1",
            {"content": [MASKED]},
            {"success": True, "past_tense_message": MASKED},
        ),
        ("system_notification", f"note {MASKED}", {"markdown": False, "meta": None}),
        ("turn_failed", f"bad {MASKED}", "agent.turn", 0, False),
    ]


async def test_turn_tee_passes_turn_id_through() -> None:
    tee = _masking_tee(RecordingSink())
    assert tee.turn_id == "turn-1"


async def test_turn_tee_returns_the_outer_outcome_untouched() -> None:
    sink = RecordingSink()
    tee = _masking_tee(sink)
    request = InputRequest(message="which?")
    call = ToolConfirmation(call_id="c1", name="shell", invocation_message="run it")
    # Compare through Any: mypy narrows `sink.outcome` to the first result's
    # type after an `is` check and then rejects the second, differently typed one.
    expected: Any = sink.outcome
    got_input: Any = await tee.request_input(request)
    got_confirm: Any = await tee.confirm_tool_call(call)
    assert got_input is expected
    assert got_confirm is expected
    assert sink.events[0][1] is request
    assert sink.events[1][1] is call


async def test_turn_tee_reports_awaiting_input_once_for_overlapping_confirmations() -> None:
    sink = RecordingSink()
    sink.gate = asyncio.Event()
    seen: list[bool] = []

    async def on_waiting(waiting: bool) -> None:
        seen.append(waiting)

    tee = _masking_tee(sink, on_waiting)
    one = asyncio.ensure_future(
        tee.confirm_tool_call(
            cast(Any, ToolConfirmation(call_id="c1", name="shell", invocation_message="run"))
        )
    )
    two = asyncio.ensure_future(
        tee.confirm_tool_call(
            cast(Any, ToolConfirmation(call_id="c1", name="shell", invocation_message="run"))
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == [True]
    sink.gate.set()
    await asyncio.gather(one, two)
    assert seen == [True, False]
    await tee.confirm_tool_call(
        cast(Any, ToolConfirmation(call_id="c1", name="shell", invocation_message="run"))
    )
    assert seen == [True, False, True, False]
