"""`PublishesChatActivity`: a provider sets a chat's activity, with a `_meta` that rides along."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import (
    AgentSessionContext,
    EchoProvider,
    EchoSession,
    PublishesChatActivity,
)

from .hosting import Wire, connect, open_session, shut

pytestmark = pytest.mark.anyio

_META = {"ahp-py/flow": {"heartbeat": {"running": 1, "tick": 1}}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingProvider(EchoProvider):
    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[EchoSession] = []

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        session = EchoSession(context)
        self.sessions.append(session)
        return session


class Rig:
    def __init__(self, host: Host, wire: Wire, provider: RecordingProvider) -> None:
        self.host, self.wire, self.provider = host, wire, provider


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    provider = RecordingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        yield Rig(host, wire, provider)
    finally:
        await shut(host, wire, serving)


async def test_chatactivity_publishes_on_chat(rig: Rig) -> None:
    chat = await open_session(rig.wire, "echo:/ca-1")
    agent = rig.provider.sessions[-1]
    publisher = agent.context.publisher
    assert isinstance(publisher, PublishesChatActivity)

    await publisher.chat_activity_changed(chat, "Running 1 step", meta=_META)
    assert await rig.wire.until(lambda: len(rig.wire.actions(chat, "chat/activityChanged")) == 1)
    sent = rig.wire.actions(chat, "chat/activityChanged")[0]
    assert sent["activity"] == "Running 1 step"
    assert sent["_meta"] == _META

    # A chat the session does not own publishes nothing.
    before = len(rig.wire.envelopes())
    await publisher.chat_activity_changed("ahp-chat:/not-mine", "x", meta=_META)
    await rig.wire.until(lambda: False, timeout=0.1)
    assert len(rig.wire.envelopes()) == before

    # None clears it: the action omits `activity` and carries no `_meta`.
    await publisher.chat_activity_changed(chat, None)
    assert await rig.wire.until(lambda: len(rig.wire.actions(chat, "chat/activityChanged")) == 2)
    cleared = rig.wire.actions(chat, "chat/activityChanged")[1]
    assert "activity" not in cleared
    assert "_meta" not in cleared
