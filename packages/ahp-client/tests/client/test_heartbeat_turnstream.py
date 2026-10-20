"""What resets a `TurnStream`'s `idle_timeout`, in virtual time (no real waiting, no tolerances)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from ahp_client.api import TurnCompleted, connect
from ahp_client.testing import FakeHost, echo_host

from .virtual_clock import VirtualClockLoop

CHAT = "ahp-chat://c/s"
SESSION_STATE = {"lifecycle": "ready", "defaultChat": CHAT, "interactivity": "full"}


def _session_host() -> FakeHost:
    host = echo_host()

    def subscribe(params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        if channel.startswith("ahp-chat:"):
            body: Any = {"turns": [], "activeTurn": None}
        elif channel == "ahp-root://":
            body = host.root_state
        else:
            body = SESSION_STATE
        return {"snapshot": {"resource": channel, "state": body, "fromSeq": host._server_seq}}

    host.on("subscribe", subscribe)
    host.on("createSession", lambda _p: {})
    host.on("disposeSession", lambda _p: {})
    return host


def _run(
    drive: Callable[[FakeHost, str, str], Coroutine[Any, Any, None]],
) -> Any:
    """Prompt with the default idle timeout while `drive(host, chat_uri, session_uri)` pushes."""

    async def scenario() -> Any:
        host = _session_host()
        await host.start()
        try:
            async with connect(transport=host.transport()) as client:
                session = await client.create_session(provider="echo")
                chat = await session.chat()
                stream = chat.prompt("hello", turn_id="t1")
                driver = asyncio.get_running_loop().create_task(drive(host, chat.uri, session.uri))
                try:
                    return await stream
                finally:
                    driver.cancel()
        finally:
            await host.stop()

    loop = VirtualClockLoop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.close()


async def _complete(host: FakeHost, chat: str) -> None:
    await host.push(chat, {"type": "chat/turnComplete", "turnId": "t1"})


async def _started(host: FakeHost, chat: str) -> None:
    await asyncio.sleep(1)
    await host.push(chat, {"type": "chat/turnStarted", "turnId": "t1"})


def test_heartbeat_turnstream_keeps_default_idle() -> None:
    async def drive(host: FakeHost, chat: str, session: str) -> None:
        await _started(host, chat)
        for tick in range(20):
            await asyncio.sleep(30)
            await host.push(
                chat,
                {
                    "type": "chat/activityChanged",
                    "activity": "Running 1 step",
                    "_meta": {"ahp-py/flow": {"heartbeat": {"running": 1, "tick": tick + 1}}},
                },
            )
        await _complete(host, chat)

    assert isinstance(_run(drive), TurnCompleted)


def test_heartbeat_turnstream_control() -> None:
    async def drive(host: FakeHost, chat: str, session: str) -> None:
        await _started(host, chat)
        await asyncio.sleep(600)
        await _complete(host, chat)

    loop_time: list[float] = []

    async def scenario() -> None:
        host = _session_host()
        await host.start()
        try:
            async with connect(transport=host.transport()) as client:
                session = await client.create_session(provider="echo")
                chat = await session.chat()
                stream = chat.prompt("hello", turn_id="t1")
                driver = asyncio.get_running_loop().create_task(drive(host, chat.uri, session.uri))
                began = asyncio.get_running_loop().time()
                try:
                    with pytest.raises(TimeoutError, match="t1"):
                        await stream
                    loop_time.append(asyncio.get_running_loop().time() - began)
                finally:
                    driver.cancel()
        finally:
            await host.stop()

    loop = VirtualClockLoop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
    # The turnStarted at 1 s is yielded and restarts the clock: 1 s + 300 s of silence.
    assert loop_time[0] == pytest.approx(301)


def test_heartbeat_turnstream_input_needed() -> None:
    async def drive(host: FakeHost, chat: str, session: str) -> None:
        await _started(host, chat)
        for n in range(10):
            await asyncio.sleep(30)
            request = {"id": f"in{n}", "chat": chat, "kind": "toolConfirmation"}
            await host.push(session, {"type": "session/inputNeededSet", "request": request})
            await asyncio.sleep(30)
            await host.push(session, {"type": "session/inputNeededRemoved", "id": f"in{n}"})
        await _complete(host, chat)

    assert isinstance(_run(drive), TurnCompleted)
