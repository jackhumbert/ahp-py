"""Draining a host so a restart does not cut conversations off.

A restart that cancels every turn at once loses the half-written answer the
user was reading. The host's half of avoiding that is `Host.drain`: stop
starting work, wait for what is running, and say what did not finish -- then
record it, so the next process can tell "restarted on purpose" from "crashed".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import AUTOMATIONS_URI, ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.automations import FileAutomationStore
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import AgentSessionContext, TurnSink, UserMessage

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Gated(EchoProvider):
    """An echo provider whose turns wait on `gate` before finishing."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.started: list[str] = []

    async def create_session(self, context: AgentSessionContext) -> Any:
        session = await super().create_session(context)
        inner = session.send_user_message

        async def slow(message: UserMessage, sink: TurnSink) -> None:
            self.started.append(str(getattr(message, "text", "")))
            await self.gate.wait()
            await inner(message, sink)

        session.send_user_message = slow  # type: ignore[method-assign]
        return session


class Resuming(Gated):
    """A gated provider whose agent sessions can pick a failed turn back up."""

    def __init__(self) -> None:
        super().__init__()
        self.resumed: list[str] = []

    async def create_session(self, context: AgentSessionContext) -> Any:
        session = await super().create_session(context)

        async def resume_turn(chat_uri: str, turn_id: str, sink: TurnSink) -> None:
            self.resumed.append(turn_id)

        session.resume_turn = resume_turn
        return session


def _host(tmp_path: Path, provider: Gated) -> Host:
    return Host(
        provider,
        LoopbackSingleUserPolicy(),
        store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
        sequence_file=tmp_path / "seq",
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


async def _chat(host: Host, client: FakeClient, uri: str) -> str:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("chats")), timeout=10.0
    )
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat: str = state["chats"][0]["resource"]
    await client.request("subscribe", {"channel": chat})
    return chat


async def _say(client: FakeClient, chat: str, text: str, turn: str, seq: int = 1) -> None:
    await client.notify(
        "dispatchAction",
        {
            "channel": chat,
            "clientSeq": seq,
            "action": {
                "type": "chat/turnStarted",
                "turnId": turn,
                "startedAt": "1970-01-01T00:00:01.000Z",
                "message": {"text": text, "origin": {"kind": "user"}},
            },
        },
    )


async def _running(host: Host, chat: str) -> None:
    for _ in range(200):
        if (host.sequencer.state_of(chat) or {}).get("activeTurn") is not None:
            await asyncio.sleep(0.05)
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the turn never started")


class TestDrain:
    async def test_it_waits_for_a_running_turn(self, tmp_path: Path) -> None:
        provider = Gated()
        host = _host(tmp_path, provider)
        try:
            client = await _client(host)
            chat = await _chat(host, client, "echo:/drain-1")
            await _say(client, chat, "finish me", "t1")
            await _running(host, chat)

            drained = asyncio.create_task(host.drain(10))
            await asyncio.sleep(0.2)
            assert host.draining
            assert not drained.done(), "drain returned with a turn still running"

            provider.gate.set()
            assert await asyncio.wait_for(drained, 5) == []
            state = host.sequencer.state_of(chat) or {}
            assert state.get("activeTurn") is None
            assert state["turns"][-1]["state"] == "complete"
        finally:
            provider.gate.set()
            await host.aclose()

    async def test_it_returns_the_chats_still_running_at_the_deadline(self, tmp_path: Path) -> None:
        provider = Gated()
        host = _host(tmp_path, provider)
        try:
            client = await _client(host)
            chat = await _chat(host, client, "echo:/drain-2")
            await _say(client, chat, "never finishes", "t1")
            await _running(host, chat)

            assert await host.drain(0.2) == [chat]
        finally:
            await host.aclose()

    async def test_an_idle_host_drains_at_once(self, tmp_path: Path) -> None:
        host = _host(tmp_path, Gated())
        try:
            assert await asyncio.wait_for(host.drain(30), 2) == []
        finally:
            await host.aclose()

    async def test_begin_drain_is_idempotent(self, tmp_path: Path) -> None:
        host = _host(tmp_path, Gated())
        try:
            assert not host.draining
            host.begin_drain()
            host.begin_drain()
            assert host.draining
        finally:
            await host.aclose()


class TestWhileDraining:
    async def test_a_new_turn_is_queued_not_started(self, tmp_path: Path) -> None:
        provider = Gated()
        host = _host(tmp_path, provider)
        try:
            client = await _client(host)
            chat = await _chat(host, client, "echo:/drain-3")
            host.begin_drain()
            await _say(client, chat, "after the restart", "t1")
            await client.collect_until(
                lambda: bool((host.sequencer.state_of(chat) or {}).get("queuedMessages")),
                timeout=5.0,
            )

            state = host.sequencer.state_of(chat) or {}
            assert state.get("activeTurn") is None
            queued = state["queuedMessages"]
            assert [(e["kind"], e["message"]["text"]) for e in queued] == [
                ("queued", "after the restart")
            ]
            # The client's optimistic turn was rejected, so it reverts it.
            rejected = [
                n["params"]
                for n in client.notifications
                if n.get("method") == "action" and n["params"].get("rejectionReason")
            ]
            assert rejected
            assert rejected[-1]["action"]["type"] == "chat/turnStarted"

            await asyncio.sleep(0.3)
            assert provider.started == [], "a turn ran while the host was draining"
            assert (host.sequencer.state_of(chat) or {}).get("activeTurn") is None
        finally:
            provider.gate.set()
            await host.aclose()

    async def test_a_queued_message_is_not_started_when_the_turn_ends(self, tmp_path: Path) -> None:
        provider = Gated()
        host = _host(tmp_path, provider)
        try:
            client = await _client(host)
            chat = await _chat(host, client, "echo:/drain-4")
            await _say(client, chat, "first", "t1")
            await _running(host, chat)
            await client.notify(
                "dispatchAction",
                {
                    "channel": chat,
                    "clientSeq": 2,
                    "action": {
                        "type": "chat/pendingMessageSet",
                        "kind": "queued",
                        "id": "q1",
                        "message": {"text": "second", "origin": {"kind": "user"}},
                    },
                },
            )
            await client.collect(seconds=0.2)

            drained = asyncio.create_task(host.drain(10))
            await asyncio.sleep(0.1)
            provider.gate.set()
            assert await asyncio.wait_for(drained, 5) == []
            await asyncio.sleep(0.3)

            state = host.sequencer.state_of(chat) or {}
            assert state.get("activeTurn") is None
            assert [e["id"] for e in state["queuedMessages"]] == ["q1"]
            assert provider.started == ["first"]
        finally:
            provider.gate.set()
            await host.aclose()


class TestInterruptedChatsArePersisted:
    async def test_the_list_reaches_the_store(self, tmp_path: Path) -> None:
        provider = Gated()
        host = _host(tmp_path, provider)
        uri = "echo:/drain-5"
        try:
            client = await _client(host)
            chat = await _chat(host, client, uri)
            await _say(client, chat, "cut off", "t1")
            await _running(host, chat)
            assert await host.drain(0.2) == [chat]
        finally:
            await host.aclose()

        stored = await FileSessionStore(tmp_path / "sessions").load_all()
        assert [s.uri for s in stored] == [uri]
        assert list(stored[0].interrupted_by_restart) == [chat]

    async def test_a_clean_drain_records_nothing(self, tmp_path: Path) -> None:
        provider = Gated()
        host = _host(tmp_path, provider)
        uri = "echo:/drain-6"
        try:
            client = await _client(host)
            chat = await _chat(host, client, uri)
            await _say(client, chat, "done", "t1")
            await _running(host, chat)
            provider.gate.set()
            assert await host.drain(5) == []
        finally:
            await host.aclose()

        stored = await FileSessionStore(tmp_path / "sessions").load_all()
        assert list(stored[0].interrupted_by_restart) == []

    async def test_the_field_round_trips_and_is_checked(self) -> None:
        from ahp_host.core.store import StoredSession

        base = StoredSession(uri="echo:/x", provider="echo", created_at="t", channels={})
        assert "interruptedByRestart" not in base.to_json()
        with_chats = StoredSession(
            uri="echo:/x",
            provider="echo",
            created_at="t",
            channels={},
            interrupted_by_restart=("c1", "c2"),
        )
        assert StoredSession.from_json(with_chats.to_json()).interrupted_by_restart == (
            "c1",
            "c2",
        )
        bad = {**base.to_json(), "interruptedByRestart": "c1"}
        with pytest.raises(ValueError, match="interruptedByRestart"):
            StoredSession.from_json(bad)


async def _cut_off(tmp_path: Path, uri: str, *, drain: bool, queued: str | None = None) -> str:
    """Run a host with one turn mid-flight, then stop it. Returns the chat URI.

    With *drain*, the stop is a deliberate restart whose deadline passed; without,
    it is whatever a crash leaves behind (the turn active in storage, nothing
    recorded about why).
    """
    provider = Resuming()
    host = _host(tmp_path, provider)
    try:
        client = await _client(host)
        chat = await _chat(host, client, uri)
        await _say(client, chat, "cut off", "t1")
        await _running(host, chat)
        if queued is not None:
            await client.notify(
                "dispatchAction",
                {
                    "channel": chat,
                    "clientSeq": 2,
                    "action": {
                        "type": "chat/pendingMessageSet",
                        "kind": "queued",
                        "id": "q1",
                        "message": {"text": queued, "origin": {"kind": "user"}},
                    },
                },
            )
            await client.collect(seconds=0.2)
        if drain:
            assert await host.drain(0.2) == [chat]
        else:
            await host._persist(host._sessions[uri])
            await host.store.flush()
    finally:
        await host.aclose()
    return chat


async def _settled(host: Host, chat: str, timeout: float = 5.0) -> dict[str, Any]:
    """The chat's state once nothing is running in it."""
    for _ in range(int(timeout / 0.02)):
        state = host.sequencer.state_of(chat) or {}
        if state.get("activeTurn") is None and not any(
            s.running(chat) for s in host._sessions.values()
        ):
            return dict(state)
        await asyncio.sleep(0.02)
    raise AssertionError("the chat never settled")


class TestRestoringAnInterruptedTurn:
    async def test_a_crash_leaves_a_resumable_turn_that_is_not_resumed(
        self, tmp_path: Path
    ) -> None:
        uri = "echo:/restore-1"
        chat = await _cut_off(tmp_path, uri, drain=False)

        provider = Resuming()
        provider.gate.set()
        host = _host(tmp_path, provider)
        try:
            assert await host.restore() == 1
            await asyncio.sleep(0.3)
            state = host.sequencer.state_of(chat) or {}
            assert state.get("activeTurn") is None
            turn = state["turns"][-1]
            assert (turn["id"], turn["state"]) == ("t1", "error")
            final = turn["responseParts"][-1]
            assert final["kind"] == "error"
            assert final["error"]["errorType"] == "host.restart"
            assert final["resumable"] is True
            assert provider.resumed == [], "a crash must not resume anything by itself"

            # Left for the user, who can still take it up.
            client = await _client(host)
            await client.request("subscribe", {"channel": chat})
            await client.notify(
                "dispatchAction",
                {
                    "channel": chat,
                    "clientSeq": 1,
                    "action": {"type": "chat/turnResume", "turnId": "t1"},
                },
            )
            await client.collect_until(lambda: bool(provider.resumed), timeout=5.0)
            state = await _settled(host, chat)
            assert provider.resumed == ["t1"]
            assert state["turns"][-1]["state"] == "complete"
        finally:
            await host.aclose()

    async def test_a_drain_deadline_resumes_the_turn_once(self, tmp_path: Path) -> None:
        uri = "echo:/restore-2"
        chat = await _cut_off(tmp_path, uri, drain=True)
        stored = await FileSessionStore(tmp_path / "sessions").load_all()
        assert list(stored[0].interrupted_by_restart) == [chat]

        provider = Resuming()
        provider.gate.set()
        host = _host(tmp_path, provider)
        try:
            assert await host.restore() == 1
            state = await _settled(host, chat)
            assert provider.resumed == ["t1"]
            assert [t["id"] for t in state["turns"]] == ["t1"]
            assert state["turns"][-1]["state"] == "complete"
        finally:
            await host.aclose()

        # Cleared as the resume began, so a third process finds nothing owed.
        stored = await FileSessionStore(tmp_path / "sessions").load_all()
        assert list(stored[0].interrupted_by_restart) == []
        again = Resuming()
        third = _host(tmp_path, again)
        try:
            await third.restore()
            await asyncio.sleep(0.3)
            assert again.resumed == []
        finally:
            await third.aclose()

    async def test_a_queued_message_runs_after_the_restart(self, tmp_path: Path) -> None:
        uri = "echo:/restore-3"
        chat = await _cut_off(tmp_path, uri, drain=True, queued="after the restart")
        stored = await FileSessionStore(tmp_path / "sessions").load_all()
        pending = stored[0].channels[chat].get("queuedMessages") or []
        assert [e["id"] for e in pending] == ["q1"]

        provider = Resuming()
        provider.gate.set()
        host = _host(tmp_path, provider)
        try:
            await host.restore()
            state = await _settled(host, chat)
            for _ in range(200):
                if provider.started:
                    break
                await asyncio.sleep(0.02)
            state = await _settled(host, chat)
            assert provider.started == ["after the restart"]
            assert not state.get("queuedMessages")
            assert state["turns"][-1]["message"]["text"] == "after the restart"
        finally:
            await host.aclose()


class TestTheFailedTurn:
    def test_unfinished_tools_are_skipped_and_an_unanswered_question_is_dropped(self) -> None:
        from ahp_host.core.host import _failed_by_restart

        state = {
            "status": 8,
            "modifiedAt": "2000-01-01T00:00:09.000Z",
            "turns": [],
            "activeTurn": {
                "id": "t1",
                "startedAt": "2000-01-01T00:00:01.000Z",
                "message": {"text": "go", "origin": {"kind": "user"}},
                "responseParts": [
                    {"kind": "markdown", "id": "m", "content": "partial"},
                    {
                        "kind": "toolCall",
                        "toolCall": {
                            "status": "running",
                            "toolCallId": "c1",
                            "toolName": "bash",
                            "displayName": "bash",
                            "invocationMessage": "run",
                            "toolInput": "{}",
                            "confirmed": "not-needed",
                        },
                    },
                    {"kind": "inputRequest", "request": {"id": "r1"}},
                ],
            },
        }
        result = _failed_by_restart(state, state["activeTurn"], resumable=False)
        assert "activeTurn" not in result
        assert result["modifiedAt"] == "2000-01-01T00:00:09.000Z"
        turn = result["turns"][0]
        assert turn["state"] == "error"
        kinds = [p["kind"] for p in turn["responseParts"]]
        assert kinds == ["markdown", "toolCall", "error"]
        assert turn["responseParts"][1]["toolCall"]["status"] == "cancelled"
        assert "resumable" not in turn["responseParts"][-1]


class TestAQueueMadeWhileDrainingSurvivesTheRestart:
    """The queue is the one thing a drain accepts and does not run.

    Nothing else about such a chat changes -- not its summary, which is what the
    normal dispatch path persists on -- so it has to be written deliberately,
    even when no turn was cut off and so nothing else asks for the write.
    """

    async def test_a_message_queued_on_an_idle_host_runs_after_the_restart(
        self, tmp_path: Path
    ) -> None:
        uri = "echo:/queue-1"
        provider = Gated()
        host = _host(tmp_path, provider)
        try:
            client = await _client(host)
            chat = await _chat(host, client, uri)
            assert await host.drain(5) == []
            await _say(client, chat, "after the restart", "t1")
            await client.collect_until(
                lambda: bool((host.sequencer.state_of(chat) or {}).get("queuedMessages")),
                timeout=5.0,
            )
        finally:
            await host.aclose()

        (stored,) = await FileSessionStore(tmp_path / "sessions").load_all()
        pending = stored.channels[chat].get("queuedMessages") or []
        assert [e["message"]["text"] for e in pending] == ["after the restart"]

        after = Resuming()
        after.gate.set()
        second = _host(tmp_path, after)
        try:
            await second.restore()
            for _ in range(200):
                if after.started:
                    break
                await asyncio.sleep(0.02)
            state = await _settled(second, chat)
            assert after.started == ["after the restart"]
            assert not state.get("queuedMessages")
        finally:
            await second.aclose()

    async def test_a_message_queued_after_a_clean_drain_of_a_running_turn(
        self, tmp_path: Path
    ) -> None:
        uri = "echo:/queue-2"
        provider = Gated()
        host = _host(tmp_path, provider)
        try:
            client = await _client(host)
            chat = await _chat(host, client, uri)
            await _say(client, chat, "first", "t1")
            await _running(host, chat)
            drained = asyncio.create_task(host.drain(10))
            await asyncio.sleep(0.1)
            provider.gate.set()
            assert await asyncio.wait_for(drained, 5) == []
            await _say(client, chat, "second", "t2", seq=2)
            await client.collect_until(
                lambda: bool((host.sequencer.state_of(chat) or {}).get("queuedMessages")),
                timeout=5.0,
            )
        finally:
            await host.aclose()

        (stored,) = await FileSessionStore(tmp_path / "sessions").load_all()
        pending = stored.channels[chat].get("queuedMessages") or []
        assert [e["message"]["text"] for e in pending] == ["second"]
        assert list(stored.interrupted_by_restart) == []


class TestAutomationRunsAreNotResumedByTheirTurn:
    """A run cut off at the deadline is settled as failed by the next start.

    Resuming its turn as well would leave an agent working that no run is
    attached to, under a catalogue that already says the run failed.
    """

    async def test_a_cut_off_run_is_failed_and_its_turn_left_for_the_user(
        self, tmp_path: Path
    ) -> None:
        def build(provider: Gated) -> Host:
            return Host(
                provider,
                LoopbackSingleUserPolicy(),
                store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
                sequence_file=tmp_path / "seq",
                automations=FileAutomationStore(tmp_path / "automations"),
            )

        automation = "ahp-automation:/nightly"
        provider = Resuming()
        host = build(provider)
        try:
            client = await _client(host)
            await client.notify(
                "dispatchAction",
                {
                    "channel": AUTOMATIONS_URI,
                    "clientSeq": 1,
                    "action": {
                        "type": "automation/createRequested",
                        "resource": automation,
                        "definition": {
                            "title": "Nightly",
                            "message": {"text": "go", "origin": {"kind": "automation"}},
                            "session": {"provider": "echo"},
                            "enabled": True,
                            "triggers": [],
                        },
                    },
                },
            )
            for _ in range(250):
                if automation in host._automations:
                    break
                await asyncio.sleep(0.02)
            run = (
                await client.request(
                    "runAutomation",
                    {"channel": AUTOMATIONS_URI, "automation": automation, "requestId": "r"},
                )
            )["result"]["resource"]
            for _ in range(250):
                if provider.started:
                    break
                await asyncio.sleep(0.02)
            assert provider.started == ["go"]
            (cut_off,) = await host.drain(0.2)
        finally:
            await host.aclose()

        (stored,) = await FileSessionStore(tmp_path / "sessions").load_all()
        assert list(stored.interrupted_by_restart) == []

        after = Resuming()
        after.gate.set()
        second = build(after)
        try:
            await second.restore()
            await asyncio.sleep(0.3)
            assert after.resumed == [], "the run's turn was resumed with no run attached"
            assert second.sequencer.state_of(run)["lifecycle"]["status"] == "failed"
            turn = (second.sequencer.state_of(cut_off) or {})["turns"][-1]
            assert turn["state"] == "error"
            assert turn["responseParts"][-1]["resumable"] is True
        finally:
            await second.aclose()

    async def test_a_later_turn_in_a_run_session_is_not_resumed_either(
        self, tmp_path: Path
    ) -> None:
        # The run is over; a follow-up turn in its session is cut off. The
        # session was made with the run's credentials, which a restored session
        # does not have, so the host does not start it again on its own.
        def build(provider: Gated) -> Host:
            return Host(
                provider,
                LoopbackSingleUserPolicy(),
                store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
                sequence_file=tmp_path / "seq",
                automations=FileAutomationStore(tmp_path / "automations"),
            )

        automation = "ahp-automation:/nightly"
        provider = Resuming()
        provider.gate.set()
        host = build(provider)
        try:
            client = await _client(host)
            await client.notify(
                "dispatchAction",
                {
                    "channel": AUTOMATIONS_URI,
                    "clientSeq": 1,
                    "action": {
                        "type": "automation/createRequested",
                        "resource": automation,
                        "definition": {
                            "title": "Nightly",
                            "message": {"text": "go", "origin": {"kind": "automation"}},
                            "session": {"provider": "echo"},
                            "enabled": True,
                            "triggers": [],
                        },
                    },
                },
            )
            for _ in range(250):
                if automation in host._automations:
                    break
                await asyncio.sleep(0.02)
            run = (
                await client.request(
                    "runAutomation",
                    {"channel": AUTOMATIONS_URI, "automation": automation, "requestId": "r"},
                )
            )["result"]["resource"]
            for _ in range(250):
                lifecycle = (host.sequencer.state_of(run) or {}).get("lifecycle") or {}
                if lifecycle.get("status") == "completed":
                    break
                await asyncio.sleep(0.02)
            session = host.sequencer.state_of(run)["primarySession"]
            chat = host.sequencer.state_of(session)["defaultChat"]
            await client.request("subscribe", {"channel": chat})
            provider.gate.clear()
            await _say(client, chat, "and then?", "t2", seq=2)
            await _running(host, chat)
            assert await host.drain(0.2) == [chat]
        finally:
            await host.aclose()

        after = Resuming()
        after.gate.set()
        second = build(after)
        try:
            await second.restore()
            await asyncio.sleep(0.3)
            assert after.resumed == [], "a run session's turn was resumed with no credentials"
            turn = (second.sequencer.state_of(chat) or {})["turns"][-1]
            assert turn["state"] == "error"
            assert turn["responseParts"][-1]["resumable"] is True
        finally:
            await second.aclose()
