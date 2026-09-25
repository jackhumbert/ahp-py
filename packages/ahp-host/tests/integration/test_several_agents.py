"""One host serving several agents.

`RootState.agents` is a list, and a machine often runs more than one agent
(Claude, and goose or opencode beside it). Each session is served by the agent
it was created with; the first agent is the default for a `createSession` that
names none, and for a restored session whose agent has since been renamed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.store import FileSessionStore
from agent_host_server.provider import EchoProvider

from .test_durability import _client

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _host(tmp_path: Path, *providers: EchoProvider) -> Host:
    return Host(
        list(providers),
        LoopbackSingleUserPolicy(),
        store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
        sequence_file=tmp_path / "seq",
    )


def _two() -> tuple[EchoProvider, EchoProvider]:
    return (
        EchoProvider(provider_id="claude", display_name="Claude"),
        EchoProvider(provider_id="goose", display_name="goose"),
    )


async def _turn(client: Any, session: str, text: str) -> dict[str, Any]:
    state = (await client.request("subscribe", {"channel": session}))["result"]["snapshot"]["state"]
    chat = state["chats"][0]["resource"]
    await client.request("subscribe", {"channel": chat})
    await client.notify(
        "dispatchAction",
        {
            "channel": chat,
            "clientSeq": 1,
            "action": {
                "type": "chat/turnStarted",
                "turnId": "t1",
                "startedAt": "1970-01-01T00:00:01.000Z",
                "message": {"text": text, "origin": {"kind": "user"}},
            },
        },
    )
    await client.collect(seconds=0.4)
    result: dict[str, Any] = (await client.request("subscribe", {"channel": chat}))["result"][
        "snapshot"
    ]["state"]
    return result


async def test_every_agent_is_listed_in_order(tmp_path: Path) -> None:
    host = _host(tmp_path, *_two())
    try:
        client = await _client(host)
        root = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]["snapshot"]
        assert [a["provider"] for a in root["state"]["agents"]] == ["claude", "goose"]
    finally:
        await host.aclose()


async def test_each_session_is_served_by_the_agent_it_names(tmp_path: Path) -> None:
    claude, goose = _two()
    host = _host(tmp_path, claude, goose)
    try:
        client = await _client(host)
        await client.request("createSession", {"channel": "goose:/a", "provider": "goose"})
        await client.request("createSession", {"channel": "claude:/b"})  # the default
        await client.collect(seconds=0.2)
        listing = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]
        providers = {i["resource"]: i["provider"] for i in listing["items"]}
        assert providers == {"goose:/a": "goose", "claude:/b": "claude"}
        transcript = await _turn(client, "goose:/a", "hello")
        assert transcript["turns"], "the goose session took no turn"
    finally:
        await host.aclose()


async def test_an_agent_the_host_does_not_serve_is_refused(tmp_path: Path) -> None:
    host = _host(tmp_path, *_two())
    try:
        client = await _client(host)
        reply = await client.request("createSession", {"channel": "x:/1", "provider": "opencode"})
        assert "error" in reply
    finally:
        await host.aclose()


def test_two_agents_may_not_share_an_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="share the id"):
        _host(tmp_path, EchoProvider(provider_id="a"), EchoProvider(provider_id="a"))


async def test_a_session_of_a_renamed_agent_is_served_by_the_default(tmp_path: Path) -> None:
    # A machine's Claude used to be `claude-studio`; its stored sessions still
    # say so. After the rename they come back, and take turns, on the default.
    first = _host(tmp_path, EchoProvider(provider_id="claude-studio"))
    try:
        client = await _client(first)
        await client.request(
            "createSession", {"channel": "claude-studio:/old", "provider": "claude-studio"}
        )
        await client.collect(seconds=0.3)
    finally:
        await first.aclose()
    await asyncio.sleep(0.1)
    stored = json.loads(next((tmp_path / "sessions").glob("*.json")).read_text())
    assert stored["provider"] == "claude-studio"

    second = _host(tmp_path, *_two())
    try:
        assert await second.restore() == 1
        client = await _client(second)
        transcript = await _turn(client, "claude-studio:/old", "still here?")
        assert transcript["turns"], "the renamed agent's session could not take a turn"
    finally:
        await second.aclose()
