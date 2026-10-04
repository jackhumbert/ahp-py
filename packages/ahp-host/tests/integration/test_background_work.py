"""Chat background work (1.0.0): `chat/backgroundWorkSet` / `Removed`.

Background work outlives the turn that started it -- a shell left running, a
subagent still going -- so it is published through the out-of-turn
`SessionPublisher`, never the turn sink, and the host never removes an entry
on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import BackgroundWork, SessionPublisher

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_SHELL = BackgroundWork(
    id="shell-1",
    kind="shell",
    label="Watch the tests",
    started_at="2026-10-04T12:00:00.000Z",
    command="pytest --watch",
    meta={"attached": True},
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = Host(EchoProvider(), LoopbackSingleUserPolicy())
    try:
        yield made
    finally:
        await made.aclose()


async def _client(host: Host) -> FakeClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "c1",
            "protocolVersions": ["1.0.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


async def _session(host: Host, client: FakeClient, uri: str) -> tuple[str, SessionPublisher]:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("chats")), timeout=10.0
    )
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat: str = state["chats"][0]["resource"]
    await client.request("subscribe", {"channel": chat})
    publisher = host._sessions[uri].publisher
    assert publisher is not None
    return chat, publisher


def _work(host: Host, chat: str) -> list[dict[str, Any]] | None:
    state = host.sequencer.state_of(chat) or {}
    work = state.get("backgroundWork")
    return None if work is None else list(work)


class TestPublishing:
    async def test_an_entry_is_published_on_the_default_chat(self, host: Host) -> None:
        client = await _client(host)
        chat, publisher = await _session(host, client, "echo:/bg-1")
        assert _work(host, chat) is None, "absent until an inventory is published"

        await publisher.background_work_set(_SHELL)
        assert _work(host, chat) == [
            {
                "id": "shell-1",
                "kind": "shell",
                "label": "Watch the tests",
                "startedAt": "2026-10-04T12:00:00.000Z",
                "command": "pytest --watch",
                "_meta": {"attached": True},
            }
        ]
        await client.collect_until(
            lambda: any(
                a["action"]["type"] == "chat/backgroundWorkSet" for a in client.actions(chat)
            )
        )

    async def test_set_upserts_and_removed_removes(self, host: Host) -> None:
        client = await _client(host)
        chat, publisher = await _session(host, client, "echo:/bg-2")
        await publisher.background_work_set(_SHELL)
        relabelled = BackgroundWork(
            id="shell-1", kind="shell", label="Still watching", command="pytest --watch"
        )
        await publisher.background_work_set(relabelled)
        work = _work(host, chat)
        assert work is not None
        assert [w["label"] for w in work] == ["Still watching"]

        await publisher.background_work_removed("shell-1")
        assert _work(host, chat) == [], "empty means no active work, not 'unknown'"

    async def test_removing_unknown_work_publishes_nothing(self, host: Host) -> None:
        client = await _client(host)
        chat, publisher = await _session(host, client, "echo:/bg-3")
        await publisher.background_work_removed("never-listed")
        await client.collect(seconds=0.2)
        assert not any(
            a["action"]["type"] == "chat/backgroundWorkRemoved" for a in client.actions(chat)
        )

    async def test_a_chat_the_session_does_not_own_is_refused(self, host: Host) -> None:
        client = await _client(host)
        _, first = await _session(host, client, "echo:/bg-4a")
        other, _ = await _session(host, client, "echo:/bg-4b")
        await first.background_work_set(_SHELL, chat=other)
        assert _work(host, other) is None

    async def test_work_survives_the_turn_that_started_it(self, host: Host) -> None:
        client = await _client(host)
        chat, publisher = await _session(host, client, "echo:/bg-5")
        await publisher.background_work_set(_SHELL)
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 1,
                "action": {
                    "type": "chat/turnStarted",
                    "turnId": "t1",
                    "startedAt": "2026-10-04T12:00:01.000Z",
                    "message": {"text": "hi", "origin": {"kind": "user"}},
                },
            },
        )
        await client.collect_until(
            lambda: any(a["action"]["type"] == "chat/turnComplete" for a in client.actions(chat))
        )
        work = _work(host, chat)
        assert work is not None
        assert [w["id"] for w in work] == ["shell-1"]


class TestValue:
    def test_a_shell_needs_its_command(self) -> None:
        with pytest.raises(ValueError, match="command"):
            BackgroundWork(id="x", kind="shell", label="x").to_wire()

    def test_a_subagent_needs_its_chat(self) -> None:
        with pytest.raises(ValueError, match="chat"):
            BackgroundWork(id="x", kind="subagent", label="x").to_wire()

    def test_an_unknown_kind_passes_through(self) -> None:
        wire = BackgroundWork(id="x", kind="workflow", label="x", started_at="t").to_wire()
        assert wire == {"id": "x", "kind": "workflow", "label": "x", "startedAt": "t"}


async def test_a_restored_chat_does_not_replay_old_work_as_running(tmp_path: Path) -> None:
    def make() -> Host:
        return Host(
            EchoProvider(),
            LoopbackSingleUserPolicy(),
            store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
            sequence_file=tmp_path / "seq",
        )

    first = make()
    try:
        client = await _client(first)
        chat, publisher = await _session(first, client, "echo:/bg-restore")
        await publisher.background_work_set(_SHELL)
        # Something that persists the session: the chat's state is saved with it.
        await first._persist(first._sessions["echo:/bg-restore"])
    finally:
        await first.aclose()

    stored = await FileSessionStore(tmp_path / "sessions").load_all()
    assert stored[0].channels[chat].get("backgroundWork"), "nothing to forget: test is vacuous"

    second = make()
    try:
        assert await second.restore() == 1
        assert _work(second, chat) is None
    finally:
        await second.aclose()
