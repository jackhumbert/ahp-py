"""Sessions the embedder opens, and turns no client started.

For an agent whose conversations live somewhere else -- another machine, another
app -- and are mirrored here: `Host.open_session` lists one without a client
creating it, `SessionPublisher.external_turn` shows a turn typed elsewhere, and
`Host.close_session` takes it away when it ends there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.store import FileSessionStore
from agent_host_server.provider.base import (
    AgentInfo,
    AgentSession,
    AgentSessionContext,
    TurnSink,
    UserMessage,
)

from .test_durability import _client

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class MirrorSession:
    def __init__(self, context: AgentSessionContext) -> None:
        self.context = context
        self.cancelled = False

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.text_delta(f"here: {message.text}")

    async def cancel(self, reason: str | None = None) -> None:
        self.cancelled = True

    async def aclose(self) -> None:
        return


class MirrorProvider:
    def __init__(self) -> None:
        self.sessions: dict[str, MirrorSession] = {}

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(provider="mirror", display_name="Mirror", description="", models=())

    async def create_session(self, context: AgentSessionContext) -> AgentSession:
        raise AssertionError("no client creates these")

    async def resume_session(self, context: AgentSessionContext) -> MirrorSession:
        session = MirrorSession(context)
        self.sessions[str((context.resume_state or {})["remote"])] = session
        return session

    async def resume_state_of(self, session: AgentSession) -> Mapping[str, Any] | None:
        assert isinstance(session, MirrorSession)
        return session.context.resume_state


def _host(tmp_path: Path, provider: MirrorProvider) -> Host:
    return Host(
        provider,
        LoopbackSingleUserPolicy(),
        store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
        sequence_file=tmp_path / "seq",
    )


async def test_an_opened_session_is_listed_and_takes_turns(tmp_path: Path) -> None:
    provider = MirrorProvider()
    host = _host(tmp_path, provider)
    try:
        client = await _client(host)
        uri = "ahp-session:/remote-1"
        assert await host.open_session(uri, title="Elsewhere", resume_state={"remote": "r1"})
        listed = (await client.request("listSessions", {}))["result"]["items"]
        assert [(s["resource"], s["title"]) for s in listed] == [(uri, "Elsewhere")]

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert state["lifecycle"] == "ready"
        chat = state["chats"][0]["resource"]
        await client.request("subscribe", {"channel": chat})

        async def typed_elsewhere(sink: TurnSink) -> None:
            await sink.text_delta("answered elsewhere")

        publisher = provider.sessions["r1"].context.publisher
        assert publisher is not None
        assert await publisher.external_turn("from my phone", typed_elsewhere)
        await client.collect(seconds=0.3)
        turns = (await client.request("subscribe", {"channel": chat}))["result"]["snapshot"][
            "state"
        ]["turns"]
        assert turns[-1]["state"] == "complete"
        assert "from my phone" in str(turns[-1])
        assert "answered elsewhere" in str(turns[-1])

        await publisher.title_changed("Renamed there")
        listed = (await client.request("listSessions", {}))["result"]["items"]
        assert listed[0]["title"] == "Renamed there"

        assert not await host.open_session(uri, title="Elsewhere", resume_state={"remote": "r1"})
        assert await host.close_session(uri)
        assert (await client.request("listSessions", {}))["result"]["items"] == []
        assert not await host.close_session(uri)
    finally:
        await host.aclose()


async def test_an_external_turn_waits_for_a_running_one(tmp_path: Path) -> None:
    provider = MirrorProvider()
    host = _host(tmp_path, provider)
    try:
        await _client(host)
        await host.open_session("ahp-session:/remote-2", title="t", resume_state={"remote": "r2"})
        publisher = provider.sessions["r2"].context.publisher
        assert publisher is not None
        release = asyncio.Event()

        async def slow(sink: TurnSink) -> None:
            await release.wait()

        assert await publisher.external_turn("one", slow)
        assert not await publisher.external_turn("two", slow)
        release.set()
    finally:
        await host.aclose()


async def test_an_opened_session_comes_back_after_a_restart(tmp_path: Path) -> None:
    first = _host(tmp_path, MirrorProvider())
    try:
        await _client(first)
        await first.open_session("ahp-session:/remote-3", title="t", resume_state={"remote": "r3"})
        await asyncio.sleep(0.2)
    finally:
        await first.aclose()

    provider = MirrorProvider()
    second = _host(tmp_path, provider)
    try:
        assert await second.restore() == 1
        assert "r3" not in provider.sessions
        # Re-opening a restored session starts its agent rather than duplicating it.
        assert not await second.open_session(
            "ahp-session:/remote-3", title="t", resume_state={"remote": "r3"}
        )
        assert "r3" in provider.sessions
        assert second.session_uris() == ["ahp-session:/remote-3"]
    finally:
        await second.aclose()
