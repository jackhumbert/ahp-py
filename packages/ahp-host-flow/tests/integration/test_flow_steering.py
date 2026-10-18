"""Steering a worker chat: interactive profiles forward it to the inner session (plan §4.5)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.flow_fakes import RecordingProvider, SteerableProvider, reset_fakes
from tests.flow_hosting import SESSION, FlowFixture, boot_flow
from tests.hosting import found, start_turn, state, wait_turn, worker_chats

pytestmark = pytest.mark.node

TOML = """
[providers.steer]
load = "tests.flow_fakes:SteerableProvider"
interactive = true

[providers.rec]
load = "tests.flow_fakes:RecordingProvider"
interactive = true

[providers.ro]
load = "tests.flow_fakes:RecordingProvider"

[routing]
default = "steer"
rules = [
  { provider = "rec", label = "rec*" },
  { provider = "ro", label = "ro*" },
]
"""

STEERING = {
    "type": "chat/pendingMessageSet",
    "kind": "steering",
    "id": "m1",
    "message": {"text": "focus on tests", "origin": {"kind": "user"}},
}


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def script(prompt: str, label: str) -> str:
    return f"```js\nreturn await agent({prompt!r}, {{label: {label!r}}})\n```"


async def worker_chat(fx: FlowFixture) -> str:
    assert await fx.wire.until(lambda: bool(worker_chats(fx.host, SESSION)))
    chat = str(worker_chats(fx.host, SESSION)[0]["resource"])
    await fx.wire.request("subscribe", {"channel": chat})
    return chat


def rejections(fx: FlowFixture) -> list[str]:
    return [
        str(m["rejectionReason"]) for m in found(fx.wire.frames, lambda m: "rejectionReason" in m)
    ]


async def test_flow_steering_forwarded(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", script("steer me", "s"))
        chat = await worker_chat(fx)
        assert await fx.wire.until(
            lambda: bool(SteerableProvider.instances and SteerableProvider.instances[0].sessions)
        )
        assert state(fx.host, chat)["interactivity"] == "full"
        await fx.wire.dispatch(chat, STEERING)
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert "steered: focus on tests" in str(turn)
        assert not state(fx.host, chat).get("queuedMessages")
        assert SteerableProvider.instances[0].sessions[0].texts == ["focus on tests"]
    finally:
        await fx.shut()


async def test_flow_steering_rejected_unsupported(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", script("hold", "rec-1"))
        chat = await worker_chat(fx)
        assert await fx.wire.until(
            lambda: bool(RecordingProvider.instances and RecordingProvider.instances[0].sessions)
        )
        await fx.wire.dispatch(chat, STEERING)
        assert await fx.wire.until(lambda: bool(rejections(fx)))
        assert rejections(fx) == ["provider 'rec' cannot take steering mid-turn"]
        RecordingProvider.instances[0].sessions[0].released.set()
        await wait_turn(fx.wire, fx.chat, "t1")
    finally:
        await fx.shut()


async def test_flow_steering_read_only(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", script("hold", "ro-1"))
        chat = await worker_chat(fx)
        assert await fx.wire.until(lambda: any(p.sessions for p in RecordingProvider.instances))
        assert state(fx.host, chat)["interactivity"] == "read-only"
        await fx.wire.dispatch(chat, STEERING)
        assert await fx.wire.until(lambda: bool(rejections(fx)))
        assert rejections(fx) == ["this worker chat is read-only"]
        for provider in RecordingProvider.instances:
            for session in provider.sessions:
                session.released.set()
        await wait_turn(fx.wire, fx.chat, "t1")
    finally:
        await fx.shut()
