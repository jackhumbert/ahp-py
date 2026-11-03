"""Reasoning timing (upstream `c301d494`, H4).

`ReasoningResponsePart.startedAt` is stamped on the producer's clock when the
part opens, and `duration` -- whole milliseconds from a monotonic clock, zero a
recorded value -- goes out as a final `chat/reasoning` with empty `content` when
the run closes: another segment opens, or the turn ends in any way. Contiguous
runs are separate parts with their own timing. A client never derives it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import TurnSink, UserMessage

from .hosting import Wire, connect, open_session, shut, state, turn_started

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Thinker(EchoSession):
    """Reasons, speaks, reasons again, then ends the way the message asks."""

    release: asyncio.Event

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.reasoning_delta("first ")
        await sink.reasoning_delta("thought")
        await sink.text_delta("between")
        await sink.reasoning_delta("second")
        if message.text == "fail":
            raise RuntimeError("boom")
        if message.text == "hang":
            await asyncio.Event().wait()


class ThinkerProvider(EchoProvider):
    async def create_session(self, context: Any) -> Thinker:
        return Thinker(context)


async def _run(text: str) -> tuple[Host, Wire, asyncio.Task[None], str]:
    host = Host(ThinkerProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    chat = await open_session(wire, f"echo:/rt-{text}")
    return host, wire, serving, chat


def _parts(host: Host, chat: str) -> list[dict[str, Any]]:
    turns = state(host, chat).get("turns") or []
    active = state(host, chat).get("activeTurn")
    pool = turns[-1]["responseParts"] if turns else (active or {}).get("responseParts", [])
    return [p for p in pool if p.get("kind") == "reasoning"]


def _timing_actions(wire: Wire, chat: str) -> list[dict[str, Any]]:
    return [
        a for a in wire.actions(chat, "chat/reasoning") if a["content"] == "" and "duration" in a
    ]


async def test_a_completed_turn_times_each_contiguous_run() -> None:
    host, wire, serving, chat = await _run("done")
    try:
        await wire.dispatch(chat, turn_started("t1", "done"))
        assert await wire.until(lambda: bool(state(host, chat).get("turns")))
        first, second = _parts(host, chat)
        assert first["content"] == "first thought"
        assert second["content"] == "second"
        for part in (first, second):
            assert str(part["startedAt"]).endswith("Z")
            assert isinstance(part["duration"], int)
            assert part["duration"] >= 0
        # Closed by the next segment (the first) and by the turn ending (the second).
        timing = _timing_actions(wire, chat)
        assert [a["partId"] for a in timing] == [first["id"], second["id"]]
        # Final and published before the turn ended.
        assert wire.envelopes(chat)[-1]["action"]["type"] == "chat/turnComplete"
    finally:
        await shut(host, wire, serving)


async def test_a_failed_turn_closes_the_open_run_first() -> None:
    host, wire, serving, chat = await _run("fail")
    try:
        await wire.dispatch(chat, turn_started("t1", "fail"))
        assert await wire.until(lambda: bool(state(host, chat).get("turns")))
        first, second = _parts(host, chat)
        assert isinstance(first["duration"], int)
        assert isinstance(second["duration"], int)
        types = [e["action"]["type"] for e in wire.envelopes(chat)]
        assert types.index("chat/error") > max(
            i for i, t in enumerate(types) if t == "chat/reasoning"
        )
    finally:
        await shut(host, wire, serving)


async def test_a_cancelled_turn_closes_the_open_run_before_it_ends() -> None:
    host, wire, serving, chat = await _run("hang")
    try:
        await wire.dispatch(chat, turn_started("t1", "hang"))
        assert await wire.until(
            lambda: len((state(host, chat).get("activeTurn") or {}).get("responseParts", [])) >= 3
        )
        await wire.dispatch(chat, {"type": "chat/turnCancelled", "turnId": "t1", "duration": 5})
        assert await wire.until(lambda: bool(state(host, chat).get("turns")))
        first, second = _parts(host, chat)
        assert isinstance(first["duration"], int)
        assert isinstance(second["duration"], int), "the run open at cancel was never closed"
    finally:
        await shut(host, wire, serving)


async def test_disposing_mid_run_does_not_lose_the_timing_either() -> None:
    host, wire, serving, chat = await _run("hang")
    try:
        await wire.dispatch(chat, turn_started("t1", "hang"))
        assert await wire.until(
            lambda: len((state(host, chat).get("activeTurn") or {}).get("responseParts", [])) >= 3
        )
        # What a client of the disposed session sees on the chat channel before it goes.
        seen = asyncio.Event()
        original = host.sequencer.publish
        closed: list[Any] = []

        async def spy(channel: str, action: Any, **kwargs: Any) -> Any:
            if channel == chat and action.get("type") in ("chat/reasoning", "chat/turnCancelled"):
                closed.append(action)
                if action["type"] == "chat/turnCancelled":
                    seen.set()
            return await original(channel, action, **kwargs)

        host.sequencer.publish = spy  # type: ignore[method-assign]
        await wire.request("disposeSession", {"channel": "echo:/rt-hang"})
        assert seen.is_set()
        timing = [a for a in closed if a["type"] == "chat/reasoning" and "duration" in a]
        assert timing
        assert isinstance(timing[-1]["duration"], int)
        assert closed.index(timing[-1]) < closed.index(
            next(a for a in closed if a["type"] == "chat/turnCancelled")
        )
    finally:
        host.sequencer.publish = original  # type: ignore[method-assign]
        await shut(host, wire, serving)
