"""`moveChat` and `ChatState.movable` (1.0.0).

Same-session moves reorder the catalogue. Cross-session and `newSession` moves
transfer the chat with its side chats, and need the provider's consent
(`TransfersChats`): a turn on a moved chat runs on another session's agent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.reducers.clock import now_iso
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.changesets import Changeset, FileChange
from ahp_host.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_CHATS = {"multipleChats": {"fork": True, "sideChat": True}}


class _Stubborn(EchoProvider):
    """An agent with no `chats_transferred`: its chats can only be reordered."""

    chats_transferred = None  # type: ignore[assignment]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = Host(EchoProvider(capabilities=_CHATS), LoopbackSingleUserPolicy())
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


async def _session(host: Host, client: FakeClient, uri: str) -> str:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("chats")), timeout=10.0
    )
    return host._sessions[uri].chat_uri


async def _chat(client: FakeClient, session: str, chat: str, **extra: Any) -> None:
    response = await client.request("createChat", {"channel": session, "chat": chat, **extra})
    assert "error" not in response, response


async def _turn(host: Host, client: FakeClient, chat: str, turn: str = "t1") -> None:
    await client.notify(
        "dispatchAction",
        {
            "channel": chat,
            "clientSeq": 1,
            "action": {
                "type": "chat/turnStarted",
                "turnId": turn,
                "startedAt": now_iso(),
                "message": {"text": "hi", "origin": {"kind": "user"}},
            },
        },
    )

    def done() -> bool:
        state = host.sequencer.state_of(chat) or {}
        finished = [t.get("id") for t in state.get("turns") or []]
        return turn in finished and state.get("activeTurn") is None

    await client.collect_until(done, timeout=5.0)


def _order(host: Host, session: str) -> list[str]:
    return [c["resource"] for c in (host.sequencer.state_of(session) or {}).get("chats", [])]


async def _move(client: FakeClient, chat: str, destination: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = await client.request(
        "moveChat", {"channel": chat, "destination": destination}
    )
    return response


def _movable(host: Host, chat: str) -> bool:
    return (host.sequencer.state_of(chat) or {}).get("movable") is True


class TestMovable:
    async def test_the_default_chat_is_not_movable_and_others_are(self, host: Host) -> None:
        client = await _client(host)
        default = await _session(host, client, "echo:/mv-1")
        await _chat(client, "echo:/mv-1", "ahp-chat:/mv-1-b")
        await client.collect(seconds=0.1)
        assert not _movable(host, default)
        assert "movable" not in (host.sequencer.state_of(default) or {}), "absent means false"
        assert _movable(host, "ahp-chat:/mv-1-b")
        entry = next(
            c
            for c in host.sequencer.state_of("echo:/mv-1")["chats"]
            if c["resource"] == "ahp-chat:/mv-1-b"
        )
        assert entry["movable"] is True, "ChatSummary.movable mirrors the chat"

    async def test_a_side_chat_moves_only_with_its_parent(self, host: Host) -> None:
        client = await _client(host)
        await _session(host, client, "echo:/mv-2")
        await _chat(client, "echo:/mv-2", "ahp-chat:/mv-2-b")
        await _turn(host, client, "ahp-chat:/mv-2-b")
        await _chat(
            client,
            "echo:/mv-2",
            "ahp-chat:/mv-2-side",
            source={"kind": "sideChat", "chat": "ahp-chat:/mv-2-b", "turnId": "t1"},
        )
        await client.collect(seconds=0.1)
        assert not _movable(host, "ahp-chat:/mv-2-side")
        refused = await _move(client, "ahp-chat:/mv-2-side", {"kind": "newSession"})
        assert refused["error"]["code"] == -32009


class TestReorder:
    async def test_first_and_after_an_anchor(self, host: Host) -> None:
        client = await _client(host)
        default = await _session(host, client, "echo:/re-1")
        b, c = "ahp-chat:/re-1-b", "ahp-chat:/re-1-c"
        await _chat(client, "echo:/re-1", b)
        await _chat(client, "echo:/re-1", c)
        assert _order(host, "echo:/re-1") == [default, b, c]

        moved = await _move(client, c, {"kind": "session", "session": "echo:/re-1"})
        assert moved["result"] == {"session": "echo:/re-1"}
        assert _order(host, "echo:/re-1") == [c, default, b], "no anchor: first"

        await _move(client, c, {"kind": "session", "session": "echo:/re-1", "after": b})
        assert _order(host, "echo:/re-1") == [default, b, c]
        assert host._sessions["echo:/re-1"].chat_uri == default, "the default is not moved"

        listing = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]
        summary = next(i for i in listing["items"] if i["resource"] == "echo:/re-1")
        assert [e["resource"] for e in summary["chats"]] == [default, b, c]

    async def test_validation(self, host: Host) -> None:
        client = await _client(host)
        default = await _session(host, client, "echo:/re-2")
        b = "ahp-chat:/re-2-b"
        await _chat(client, "echo:/re-2", b)
        here = {"kind": "session", "session": "echo:/re-2"}
        assert (await _move(client, b, {**here, "after": b}))["error"]["code"] == -32602
        missing = await _move(client, b, {**here, "after": "ahp-chat:/elsewhere"})
        assert missing["error"]["code"] == -32008
        nowhere = await _move(client, b, {"kind": "session", "session": "echo:/nope"})
        assert nowhere["error"]["code"] == -32001
        assert (await _move(client, b, {"kind": "teleport"}))["error"]["code"] == -32602
        assert (await _move(client, default, here))["error"]["code"] == -32009
        assert _order(host, "echo:/re-2") == [default, b], "a refused move changes nothing"


class TestTransfer:
    async def test_a_chat_and_its_side_chat_move_together(self, host: Host) -> None:
        client = await _client(host)
        await _session(host, client, "echo:/tx-a")
        target_default = await _session(host, client, "echo:/tx-b")
        b, side = "ahp-chat:/tx-b1", "ahp-chat:/tx-side"
        await _chat(client, "echo:/tx-a", b)
        await _turn(host, client, b)
        await _chat(
            client,
            "echo:/tx-a",
            side,
            source={"kind": "sideChat", "chat": b, "turnId": "t1"},
        )
        publisher = host._sessions["echo:/tx-a"].publisher
        assert publisher is not None
        changeset = await publisher.changes_published(
            Changeset(label="mine"),
            [FileChange(uri="file:///w/a", before=b"", after=b"x\n")],
            chat=b,
        )
        terminal = await publisher.open_terminal("build", chat=b)

        moved = await _move(
            client, b, {"kind": "session", "session": "echo:/tx-b", "after": target_default}
        )
        assert moved["result"] == {"session": "echo:/tx-b"}

        assert b not in _order(host, "echo:/tx-a")
        assert side not in _order(host, "echo:/tx-a")
        assert _order(host, "echo:/tx-b") == [target_default, b, side]
        provider = host.providers["echo"]
        assert provider.transfers[-1] == ((b, side), "echo:/tx-a", "echo:/tx-b")  # type: ignore[attr-defined]
        # The chat kept its URI, transcript and origin-free identity.
        assert (host.sequencer.state_of(b) or {})["turns"]
        assert (host.sequencer.state_of(side) or {})["origin"]["chat"] == b
        # What the host keeps per chat went with it.
        assert host._sessions["echo:/tx-b"].changeset_chats[changeset] == b
        assert host.sequencer.state_of(terminal.resource)["claim"]["session"] == "echo:/tx-b"
        # A turn on the moved chat now runs in its new session.
        await _turn(host, client, b, turn="t2")
        assert len((host.sequencer.state_of(b) or {})["turns"]) == 2

    async def test_to_a_new_session(self, host: Host) -> None:
        client = await _client(host)
        await _session(host, client, "echo:/tx-n")
        b = "ahp-chat:/tx-n-b"
        await _chat(client, "echo:/tx-n", b)
        await _turn(host, client, b)

        moved = await _move(client, b, {"kind": "newSession"})
        created = moved["result"]["session"]
        assert created != "echo:/tx-n"
        assert b not in _order(host, "echo:/tx-n")
        new_state = host.sequencer.state_of(created) or {}
        assert new_state["defaultChat"] == b
        assert _order(host, created) == [b]
        await client.collect_until(lambda: not _movable(host, b))
        assert not _movable(host, b), "it is now a default chat"
        assert (host.sequencer.state_of(b) or {})["turns"], "the transcript came along"
        listing = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]
        assert created in [i["resource"] for i in listing["items"]]

    async def test_a_running_turn_is_a_conflict(self) -> None:
        host = Host(EchoProvider(capabilities=_CHATS, delay=0.5), LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            await _session(host, client, "echo:/tx-busy")
            b = "ahp-chat:/tx-busy-b"
            await _chat(client, "echo:/tx-busy", b)
            await client.notify(
                "dispatchAction",
                {
                    "channel": b,
                    "clientSeq": 1,
                    "action": {
                        "type": "chat/turnStarted",
                        "turnId": "t1",
                        "startedAt": now_iso(),
                        "message": {"text": "slow", "origin": {"kind": "user"}},
                    },
                },
            )
            await client.collect_until(lambda: bool(host._sessions["echo:/tx-busy"].running(b)))
            busy = await _move(client, b, {"kind": "newSession"})
            assert busy["error"]["code"] == -32011
        finally:
            await host.aclose()

    async def test_an_agent_that_cannot_transfer_can_still_reorder(self) -> None:
        host = Host(_Stubborn(capabilities=_CHATS), LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            default = await _session(host, client, "echo:/st-a")
            await _session(host, client, "echo:/st-b")
            b = "ahp-chat:/st-a-b"
            await _chat(client, "echo:/st-a", b)
            refused = await _move(client, b, {"kind": "session", "session": "echo:/st-b"})
            assert refused["error"]["code"] == -32009
            assert _order(host, "echo:/st-a") == [default, b]
            here = await _move(client, b, {"kind": "session", "session": "echo:/st-a"})
            assert here["result"] == {"session": "echo:/st-a"}
            assert _order(host, "echo:/st-a") == [b, default]
        finally:
            await host.aclose()
