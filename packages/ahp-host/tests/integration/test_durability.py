"""Sessions that survive a restart.

The property is not "a file was written" — it is that a client reconnecting
across a restart finds its session, its transcript, and a `serverSeq` that has
not gone backwards. The last one is the trap: the reference TypeScript client
records `lastSeenServerSeq` with a MAX, so a counter that restarts at zero
leaves it permanently ahead and unable to replay for the life of the host.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.store import FileSessionStore
from agent_host_server.provider import EchoProvider
from agent_host_server.provider.echo import EchoSession

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _host(tmp_path: Path, *, configurable: bool = False, **extra: Any) -> Host:
    return Host(
        EchoProvider(configurable=configurable),
        LoopbackSingleUserPolicy(),
        store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
        sequence_file=tmp_path / "seq",
        **extra,
    )


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
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


class TestRestart:
    async def test_a_session_and_its_transcript_come_back(self, tmp_path: Path) -> None:
        first = _host(tmp_path)
        uri = "echo:/durable-1"
        try:
            client = await _client(first)
            await client.request("createSession", {"channel": uri, "provider": "echo"})
            await client.collect(seconds=0.3)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
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
                        "message": {"text": "remember me", "origin": {"kind": "user"}},
                    },
                },
            )
            await client.collect(seconds=0.5)
        finally:
            await first.aclose()

        second = _host(tmp_path)
        try:
            assert await second.restore() == 1
            client = await _client(second)
            listing = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]
            assert [i["resource"] for i in listing["items"]] == [uri]

            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            chat = state["chats"][0]["resource"]
            transcript = (await client.request("subscribe", {"channel": chat}))["result"][
                "snapshot"
            ]["state"]
            assert transcript["turns"], "the transcript did not survive"
        finally:
            await second.aclose()

    async def test_a_restored_session_takes_a_turn_with_its_current_config(
        self, tmp_path: Path
    ) -> None:
        """Restored sessions resume their agent on the first turn -- with the
        config the state holds now, including a mid-session change."""
        first = _host(tmp_path, configurable=True)
        uri = "echo:/durable-resume"
        try:
            client = await _client(first)
            await client.request("createSession", {"channel": uri, "provider": "echo"})
            await client.collect(seconds=0.3)
            await client.request("subscribe", {"channel": uri})
            await client.notify(
                "dispatchAction",
                {
                    "channel": uri,
                    "clientSeq": 1,
                    "action": {"type": "session/configChanged", "config": {"prefix": "Heard:"}},
                },
            )
            await client.collect(seconds=0.3)
        finally:
            await first.aclose()

        second = _host(tmp_path, configurable=True)
        try:
            assert await second.restore() == 1
            client = await _client(second)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            chat = state["chats"][0]["resource"]
            await client.request("subscribe", {"channel": chat})
            await client.notify(
                "dispatchAction",
                {
                    "channel": chat,
                    "clientSeq": 1,
                    "action": {
                        "type": "chat/turnStarted",
                        "turnId": "t2",
                        "startedAt": "1970-01-01T00:00:02.000Z",
                        "message": {"text": "again", "origin": {"kind": "user"}},
                    },
                },
            )
            await client.collect(seconds=0.5)
            kinds = [e["action"]["type"] for e in client.actions(chat)]
            assert "chat/turnFailed" not in kinds, "a restored session could not take a turn"
            deltas = "".join(
                e["action"].get("content", "")
                for e in client.actions(chat)
                if e["action"]["type"] == "chat/delta"
            )
            assert deltas.startswith("Heard:"), deltas
        finally:
            await second.aclose()

    async def test_server_seq_does_not_go_backwards(self, tmp_path: Path) -> None:
        """The trap: the reference client takes a MAX of the sequence, so a
        counter that restarts at zero can never replay again."""
        first = _host(tmp_path)
        try:
            client = await _client(first)
            await client.request(
                "createSession", {"channel": "echo:/durable-2", "provider": "echo"}
            )
            await client.collect(seconds=0.3)
            reached = first.sequencer.server_seq
        finally:
            await first.aclose()

        second = _host(tmp_path)
        try:
            await second.restore()
            client = await _client(second)
            await client.request(
                "createSession", {"channel": "echo:/durable-3", "provider": "echo"}
            )
            await client.collect(seconds=0.3)
            assert second.sequencer.server_seq > reached
        finally:
            await second.aclose()

    async def test_an_interrupted_turn_is_not_restored_as_active(self, tmp_path: Path) -> None:
        """ "In-progress turns SHOULD be considered failed." The partial response
        is kept -- the user should see what the agent had said -- but a turn
        nothing is running is not active."""
        first = _host(tmp_path)
        uri = "echo:/durable-4"
        try:
            client = await _client(first)
            await client.request("createSession", {"channel": uri, "provider": "echo"})
            await client.collect(seconds=0.3)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            chat = state["chats"][0]["resource"]
            # Force an active turn into the persisted state.
            await first.sequencer.publish(
                chat,
                {
                    "type": "chat/turnStarted",
                    "turnId": "stuck",
                    "startedAt": "1970-01-01T00:00:01.000Z",
                    "message": {"text": "mid-flight", "origin": {"kind": "user"}},
                },
            )
            await first.store.flush()
            session = first._sessions[uri]
            await first._persist(session)
            await first.store.flush()
        finally:
            await first.aclose()

        second = _host(tmp_path)
        try:
            await second.restore()
            client = await _client(second)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            chat = state["chats"][0]["resource"]
            transcript = (await client.request("subscribe", {"channel": chat}))["result"][
                "snapshot"
            ]["state"]
            assert transcript.get("activeTurn") is None
        finally:
            await second.aclose()

    async def test_a_stored_turn_without_a_message_origin_is_given_one(
        self, tmp_path: Path
    ) -> None:
        """External turns were once stored with no `Message.origin`, which the
        schema requires; a chat holding one could not be decoded by a strict
        client. Restoring gives them the origin they were typed with."""
        first = _host(tmp_path)
        uri = "echo:/durable-origin"
        try:
            client = await _client(first)
            await client.request("createSession", {"channel": uri, "provider": "echo"})
            await client.collect(seconds=0.3)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            chat = state["chats"][0]["resource"]
            await first.sequencer.publish(
                chat,
                {
                    "type": "chat/turnStarted",
                    "turnId": "old",
                    "startedAt": "1970-01-01T00:00:01.000Z",
                    "message": {"text": "typed elsewhere"},
                },
            )
            await first.sequencer.publish(chat, {"type": "chat/turnComplete", "turnId": "old"})
            session = first._sessions[uri]
            await first._persist(session)
            await first.store.flush()
        finally:
            await first.aclose()

        second = _host(tmp_path)
        try:
            await second.restore()
            client = await _client(second)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            chat = state["chats"][0]["resource"]
            transcript = (await client.request("subscribe", {"channel": chat}))["result"][
                "snapshot"
            ]["state"]
            assert [t["message"] for t in transcript["turns"]] == [
                {"text": "typed elsewhere", "origin": {"kind": "user"}}
            ]
        finally:
            await second.aclose()

    async def test_a_disposed_session_does_not_come_back(self, tmp_path: Path) -> None:
        first = _host(tmp_path)
        try:
            client = await _client(first)
            await client.request(
                "createSession", {"channel": "echo:/durable-5", "provider": "echo"}
            )
            await client.collect(seconds=0.3)
            await client.request("disposeSession", {"channel": "echo:/durable-5"})
            await first.store.flush()
        finally:
            await first.aclose()

        second = _host(tmp_path)
        try:
            assert await second.restore() == 0
        finally:
            await second.aclose()

    async def test_the_policy_can_refuse_a_restore(self, tmp_path: Path) -> None:
        """The gate on the one thing a restart makes possible: state written by
        a host that may have been configured differently."""
        first = _host(tmp_path)
        try:
            client = await _client(first)
            await client.request(
                "createSession", {"channel": "echo:/durable-6", "provider": "echo"}
            )
            await client.collect(seconds=0.3)
        finally:
            await first.aclose()

        class NoRestores(LoopbackSingleUserPolicy):
            def may_restore_session(self, session: Any) -> bool:
                return False

        second = Host(
            EchoProvider(),
            NoRestores(),
            store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
        )
        try:
            assert await second.restore() == 0
        finally:
            await second.aclose()

    async def test_restore_is_idempotent(self, tmp_path: Path) -> None:
        first = _host(tmp_path)
        try:
            client = await _client(first)
            await client.request(
                "createSession", {"channel": "echo:/durable-7", "provider": "echo"}
            )
            await client.collect(seconds=0.3)
        finally:
            await first.aclose()

        second = _host(tmp_path)
        try:
            assert await second.restore() == 1
            assert await second.restore() == 0
            assert second.counters()["sessions"] == 1
        finally:
            await second.aclose()

    async def test_a_host_with_no_store_keeps_nothing(self, tmp_path: Path) -> None:
        """The default. A host whose sessions do not outlive it should not be
        writing a transcript to a path nobody chose."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            await client.request(
                "createSession", {"channel": "echo:/durable-8", "provider": "echo"}
            )
            await client.collect(seconds=0.3)
            assert await host.restore() == 0
            assert not list(tmp_path.iterdir())
        finally:
            await host.aclose()


class TestRestoredConfigSchema:
    async def test_a_restored_session_is_described_by_the_provider_as_it_is_now(
        self, tmp_path: Path
    ) -> None:
        """A property the provider has since made `sessionMutable` must become
        changeable on sessions created before, while keeping their values."""
        first = _host(tmp_path, configurable=True)
        uri = "echo:/durable-schema"
        try:
            client = await _client(first)
            await client.request(
                "createSession",
                {"channel": uri, "provider": "echo", "config": {"style": "shout"}},
            )
            await client.collect(seconds=0.3)
        finally:
            await first.aclose()

        # Simulate an older provider having stored `prefix` as creation-time only.
        stored = next((tmp_path / "sessions").glob("*.json"))
        data = json.loads(stored.read_text())
        for channel in data["channels"].values():
            props = channel.get("config", {}).get("schema", {}).get("properties", {})
            if "prefix" in props:
                props["prefix"].pop("sessionMutable", None)
        stored.write_text(json.dumps(data))

        second = _host(tmp_path, configurable=True)
        try:
            assert await second.restore() == 1
            client = await _client(second)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            assert state["config"]["schema"]["properties"]["prefix"]["sessionMutable"] is True
            assert state["config"]["values"]["style"] == "shout"
        finally:
            await second.aclose()

    async def test_a_config_change_from_the_agent_reaches_clients_and_is_saved(
        self, tmp_path: Path
    ) -> None:
        """`SessionPublisher.config_changed`: the agent's own setting moved
        elsewhere (a mode switched on a phone), so clients and the saved state
        must show what is actually in force."""
        first = _host(tmp_path, configurable=True)
        uri = "echo:/durable-agent-config"
        try:
            client = await _client(first)
            await client.request("createSession", {"channel": uri, "provider": "echo"})
            await client.collect(seconds=0.3)
            await client.request("subscribe", {"channel": uri})
            agent = first._sessions[uri].agent_session
            assert isinstance(agent, EchoSession)
            publisher = agent.context.publisher
            assert publisher is not None

            await publisher.config_changed({"prefix": "Changed there:"})
            await client.collect(seconds=0.3)
            changes = [
                e["action"]
                for e in client.actions(uri)
                if e["action"]["type"] == "session/configChanged"
            ]
            assert changes == [
                {"type": "session/configChanged", "config": {"prefix": "Changed there:"}}
            ]
            assert agent.config_overrides == {}, "the agent heard its own change"
        finally:
            await first.aclose()

        second = _host(tmp_path, configurable=True)
        try:
            assert await second.restore() == 1
            client = await _client(second)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            assert state["config"]["values"]["prefix"] == "Changed there:"
        finally:
            await second.aclose()

    async def test_archiving_reaches_the_agent_and_is_saved(self, tmp_path: Path) -> None:
        """`ArchivesSessions`: a client files the session away (or back), and an
        agent whose session also lives elsewhere does the same there."""
        host = _host(tmp_path)
        uri = "echo:/durable-archive"
        try:
            client = await _client(host)
            await client.request("createSession", {"channel": uri, "provider": "echo"})
            await client.collect(seconds=0.3)
            await client.request("subscribe", {"channel": uri})
            heard: list[bool] = []

            async def archived_changed(is_archived: bool) -> None:
                heard.append(is_archived)

            agent = host._sessions[uri].agent_session
            agent.archived_changed = archived_changed  # type: ignore[union-attr]
            for seq, archived in enumerate((True, False), start=1):
                await client.notify(
                    "dispatchAction",
                    {
                        "channel": uri,
                        "clientSeq": seq,
                        "action": {"type": "session/isArchivedChanged", "isArchived": archived},
                    },
                )
            await client.collect(seconds=0.3)
            assert heard == [True, False]
            state = host.sequencer.state_of(uri)
            assert isinstance(state, dict)
            assert state["status"] & (1 << 6) == 0, "the reducer did not unarchive it"
        finally:
            await host.aclose()
