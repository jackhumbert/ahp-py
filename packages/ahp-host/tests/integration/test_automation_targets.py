"""Automation run targets (`AutomationDefinition.target`, upstream 1.2.0, unreleased).

`previousSession`: a run continues the session of the run before it while that
session is usable, and otherwise creates one. `chat`: every run sends to one
existing chat and never creates a session. Either way a reused session gets
the message as a **queued** pending message, and the run's lifecycle follows
that message: pending while it waits, running once a turn starts from it, then
that turn's outcome; withdrawn means cancelled, cancelling withdraws it, and a
session or chat that goes first fails the run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ahp_protocol.channels import AUTOMATIONS_URI

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.automations import InMemoryAutomationStore
from ahp_host.provider import EchoProvider

from .test_automations import AUTOMATION, _client, _create, _definition, _entry, _status, _until
from .test_host_end_to_end import FakeClient
from .test_move_chat import _chat, _move, _session, _turn

pytestmark = pytest.mark.anyio

_CHATS = {"multipleChats": {"fork": True, "sideChat": True}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _host(delay: float = 0.0) -> Host:
    return Host(
        EchoProvider(delay=delay, capabilities=_CHATS),
        LoopbackSingleUserPolicy(),
        automations=InMemoryAutomationStore(),
    )


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = _host()
    try:
        yield made
    finally:
        await made.aclose()


@pytest.fixture
async def slow_host() -> AsyncIterator[Host]:
    made = _host(delay=3.0)
    try:
        yield made
    finally:
        await made.aclose()


_counter = 0


async def _run(client: FakeClient) -> str:
    global _counter
    _counter += 1
    reply = await client.request(
        "runAutomation",
        {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": f"r{_counter}"},
    )
    return str(reply["result"]["resource"])


def _run_state(host: Host, run: str) -> dict[str, Any]:
    return dict(host.sequencer.state_of(run) or {})


def _queued_ids(host: Host, chat: str) -> list[Any]:
    state = host.sequencer.state_of(chat) or {}
    return [entry.get("id") for entry in state.get("queuedMessages") or []]


def _turn_texts(host: Host, chat: str) -> list[str]:
    state = host.sequencer.state_of(chat) or {}
    return [t["message"]["text"] for t in state.get("turns") or []]


def _automation_sessions(host: Host) -> list[str]:
    return [
        uri
        for uri in host._sessions
        if ((host.sequencer.state_of(uri) or {}).get("origin") or {}).get("kind") == "automation"
    ]


async def _busy(host: Host, client: FakeClient, chat: str) -> None:
    """Start a user turn in *chat* and return while it runs."""
    await client.notify(
        "dispatchAction",
        {
            "channel": chat,
            "clientSeq": 1,
            "action": {
                "type": "chat/turnStarted",
                "turnId": "user-turn",
                "startedAt": "1970-01-01T00:00:01.000Z",
                "message": {"text": "mine", "origin": {"kind": "user"}},
            },
        },
    )
    await _until(lambda: (host.sequencer.state_of(chat) or {}).get("activeTurn") is not None)


async def test_the_targets_are_advertised(host: Host) -> None:
    _, result = await _client(host)
    assert result["automations"]["targets"] == {"previousSession": {}, "chat": {}}


class TestPreviousSession:
    async def test_the_second_run_continues_the_first_runs_session(self, host: Host) -> None:
        client, _ = await _client(host)
        await _create(client, host, _definition(target={"kind": "previousSession"}))
        first = await _run(client)
        await _until(lambda: _status(host, first) == "completed")
        (session,) = _automation_sessions(host)
        assert _run_state(host, first)["primarySession"] == session

        second = await _run(client)
        await _until(lambda: _status(host, second) == "completed")
        assert _automation_sessions(host) == [session], "no second session"
        state = _run_state(host, second)
        assert state["primarySession"] == session
        assert state["sessions"] == [session]
        chat = host._sessions[session].chat_uri
        assert _turn_texts(host, chat) == ["check the build", "check the build"]
        assert _queued_ids(host, chat) == []
        # The session still names the run that created it.
        assert host.sequencer.state_of(session)["origin"]["run"] == first

    async def test_overlapping_runs_share_one_session(self) -> None:
        host = _host(delay=0.3)
        try:
            client, _ = await _client(host)
            await _create(client, host, _definition(target={"kind": "previousSession"}))
            first = await _run(client)
            second = await _run(client)
            await _until(lambda: _status(host, second) == "completed", timeout=10)
            assert _status(host, first) == "completed"
            (session,) = _automation_sessions(host)
            assert _run_state(host, second)["primarySession"] == session
            # One after the other, in the one chat: queued, never steered in.
            chat = host._sessions[session].chat_uri
            assert _turn_texts(host, chat) == ["check the build", "check the build"]
        finally:
            await host.aclose()

    async def test_an_archived_session_starts_the_conversation_over(self, host: Host) -> None:
        client, _ = await _client(host)
        await _create(client, host, _definition(target={"kind": "previousSession"}))
        first = await _run(client)
        await _until(lambda: _status(host, first) == "completed")
        (session,) = _automation_sessions(host)
        await client.notify(
            "dispatchAction",
            {
                "channel": session,
                "clientSeq": 2,
                "action": {"type": "session/isArchivedChanged", "isArchived": True},
            },
        )
        await _until(lambda: host._session_is_archived(host._sessions[session]))
        second = await _run(client)
        await _until(lambda: _status(host, second) == "completed")
        fresh = _run_state(host, second)["primarySession"]
        assert fresh != session
        third = await _run(client)
        await _until(lambda: _status(host, third) == "completed")
        assert _run_state(host, third)["primarySession"] == fresh

    async def test_a_removed_session_starts_the_conversation_over(self, host: Host) -> None:
        client, _ = await _client(host)
        await _create(client, host, _definition(target={"kind": "previousSession"}))
        first = await _run(client)
        await _until(lambda: _status(host, first) == "completed")
        (session,) = _automation_sessions(host)
        await client.request("disposeSession", {"channel": session})
        second = await _run(client)
        await _until(lambda: _status(host, second) == "completed")
        assert _run_state(host, second)["primarySession"] != session


class TestChat:
    async def test_runs_send_to_the_chat_and_the_host_names_its_session(self, host: Host) -> None:
        client, _ = await _client(host)
        chat = await _session(host, client, "echo:/t-chat")
        target = {"kind": "chat", "chat": chat, "session": "echo:/stale"}
        await _create(client, host, _definition(target=target))
        assert _entry(host)["definition"]["target"] == {
            "kind": "chat",
            "chat": chat,
            "session": "echo:/t-chat",
        }
        run = await _run(client)
        await _until(lambda: _status(host, run) == "completed")
        assert _automation_sessions(host) == [], "a chat run never creates a session"
        assert _run_state(host, run)["primarySession"] == "echo:/t-chat"
        assert _turn_texts(host, chat) == ["check the build"]

    async def test_a_busy_chat_runs_it_after_the_current_turn(self, slow_host: Host) -> None:
        host = slow_host
        client, _ = await _client(host)
        chat = await _session(host, client, "echo:/t-busy")
        await _create(client, host, _definition(target={"kind": "chat", "chat": chat}))
        await _busy(host, client, chat)
        run = await _run(client)
        await _until(lambda: _queued_ids(host, chat) == [run])
        assert _status(host, run) == "pending"
        assert _run_state(host, run)["primarySession"] == "echo:/t-busy"
        assert (host.sequencer.state_of(chat) or {})["activeTurn"]["id"] == "user-turn", (
            "queued, never steered into the running turn"
        )
        await _until(lambda: _status(host, run) == "running", timeout=10)
        assert _queued_ids(host, chat) == []

    async def test_cancelling_a_waiting_run_withdraws_only_its_message(
        self, slow_host: Host
    ) -> None:
        host = slow_host
        client, _ = await _client(host)
        chat = await _session(host, client, "echo:/t-cancel")
        await _create(client, host, _definition(target={"kind": "chat", "chat": chat}))
        await _busy(host, client, chat)
        run = await _run(client)
        await _until(lambda: _queued_ids(host, chat) == [run])
        await client.notify(
            "dispatchAction",
            {
                "channel": run,
                "clientSeq": 3,
                "action": {"type": "automationRun/cancelRequested"},
            },
        )
        await _until(lambda: _status(host, run) == "cancelled")
        assert _queued_ids(host, chat) == []
        assert (host.sequencer.state_of(chat) or {})["activeTurn"]["id"] == "user-turn", (
            "the user's turn goes on"
        )

    async def test_a_withdrawn_message_cancels_the_run(self, slow_host: Host) -> None:
        host = slow_host
        client, _ = await _client(host)
        chat = await _session(host, client, "echo:/t-withdraw")
        await _create(client, host, _definition(target={"kind": "chat", "chat": chat}))
        await _busy(host, client, chat)
        run = await _run(client)
        await _until(lambda: _queued_ids(host, chat) == [run])
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 4,
                "action": {"type": "chat/pendingMessageRemoved", "kind": "queued", "id": run},
            },
        )
        await _until(lambda: _status(host, run) == "cancelled")

    async def test_a_session_removed_under_a_waiting_run_fails_it(self, slow_host: Host) -> None:
        host = slow_host
        client, _ = await _client(host)
        chat = await _session(host, client, "echo:/t-gone")
        await _create(client, host, _definition(target={"kind": "chat", "chat": chat}))
        await _busy(host, client, chat)
        run = await _run(client)
        await _until(lambda: _queued_ids(host, chat) == [run])
        await client.request("disposeSession", {"channel": "echo:/t-gone"})
        await _until(lambda: _status(host, run) == "failed")
        assert "removed" in _run_state(host, run)["lifecycle"]["error"]["message"]

    async def test_a_chat_that_is_gone_fails_the_run(self, host: Host) -> None:
        client, _ = await _client(host)
        await _session(host, client, "echo:/t-s")
        side = "ahp-chat:/t-side"
        await _chat(client, "echo:/t-s", side)
        await _create(client, host, _definition(target={"kind": "chat", "chat": side}))
        await client.request("disposeChat", {"channel": side})
        run = await _run(client)
        await _until(lambda: _status(host, run) == "failed")
        assert _automation_sessions(host) == []

    async def test_a_moved_chat_moves_the_target(self, host: Host) -> None:
        client, _ = await _client(host)
        await _session(host, client, "echo:/t-from")
        await _session(host, client, "echo:/t-to")
        side = "ahp-chat:/t-moving"
        await _chat(client, "echo:/t-from", side)
        await _create(client, host, _definition(target={"kind": "chat", "chat": side}))
        assert _entry(host)["definition"]["target"]["session"] == "echo:/t-from"
        moved = await _move(client, side, {"kind": "session", "session": "echo:/t-to"})
        assert "error" not in moved, moved
        await _until(lambda: _entry(host)["definition"]["target"]["session"] == "echo:/t-to")
        run = await _run(client)
        await _until(lambda: _status(host, run) == "completed")
        assert _run_state(host, run)["primarySession"] == "echo:/t-to"

    async def test_an_unknown_chat_is_rejected_on_update(self, host: Host) -> None:
        client, _ = await _client(host)
        await _create(client, host, _definition())
        await client.request("subscribe", {"channel": AUTOMATIONS_URI})
        await client.notify(
            "dispatchAction",
            {
                "channel": AUTOMATIONS_URI,
                "clientSeq": 9,
                "action": {
                    "type": "automation/updateRequested",
                    "resource": AUTOMATION,
                    "changes": {"target": {"kind": "chat", "chat": "ahp-chat:/nowhere"}},
                },
            },
        )
        await asyncio.sleep(0.2)
        assert "target" not in _entry(host)["definition"]


async def test_a_message_left_behind_by_a_stopped_process_is_withdrawn(host: Host) -> None:
    """A run waiting at shutdown is failed on restore; its queued message must
    not then run as a turn that no run follows."""
    client, _ = await _client(host)
    chat = await _session(host, client, "echo:/t-orphan")
    await _create(client, host, _definition())
    run = await _run(client)
    await _until(lambda: _status(host, run) == "completed")
    await host.sequencer.publish(
        chat,
        {
            "type": "chat/pendingMessageSet",
            "kind": "queued",
            "id": run,
            "message": {"text": "orphan", "origin": {"kind": "automation"}},
        },
    )
    await _turn(host, client, chat)
    await _until(lambda: _queued_ids(host, chat) == [])
    assert "orphan" not in _turn_texts(host, chat)
