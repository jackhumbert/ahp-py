"""`chat/moved` (1.1.0): the chat's new session after a cross-session `moveChat`.

"After a cross-session move the host dispatches `chat/moved` on the chat's channel
with its new session, `provider` and `config.schema`" -- for the chat and every
descendant it took along, after the source's `session/chatRemoved` and the
destination's `session/chatAdded` (the spec's publishing order). Before a chat can
be backed by another agent every chat's agent matches its session's, so neither
`provider` nor `schema` is carried, and the reducer clears both.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from ahp_protocol.reducers import chat_reducer

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider

from .test_host_end_to_end import FakeClient
from .test_move_chat import _chat, _client, _move, _session, _turn

pytestmark = pytest.mark.anyio

_CHATS = {"multipleChats": {"fork": True, "sideChat": True}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = Host(EchoProvider(configurable=True, capabilities=_CHATS), LoopbackSingleUserPolicy())
    try:
        yield made
    finally:
        await made.aclose()


def _moved(client: FakeClient, chat: str) -> list[dict[str, Any]]:
    return [e for e in client.actions(chat) if e["action"]["type"] == "chat/moved"]


def _seq(client: FakeClient, channel: str, kind: str) -> int:
    (found,) = [e for e in client.actions(channel) if e["action"]["type"] == kind]
    return int(found["serverSeq"])


async def test_a_cross_session_move_dispatches_chat_moved_after_the_catalogue(host: Host) -> None:
    client = await _client(host)
    await _session(host, client, "echo:/cm-a")
    await _session(host, client, "echo:/cm-b")
    b = "ahp-chat:/cm-b1"
    await _chat(client, "echo:/cm-a", b)
    await client.request("subscribe", {"channel": "echo:/cm-a"})
    await client.request("subscribe", {"channel": "echo:/cm-b"})
    await client.request("subscribe", {"channel": b})

    moved = await _move(client, b, {"kind": "session", "session": "echo:/cm-b"})
    assert moved["result"] == {"session": "echo:/cm-b"}
    await client.collect_until(lambda: bool(_moved(client, b)))

    (envelope,) = _moved(client, b)
    assert envelope["action"] == {"type": "chat/moved", "session": "echo:/cm-b"}, (
        "no provider and no schema: the chat's agent is its session's"
    )
    assert _seq(client, "echo:/cm-a", "session/chatRemoved") < envelope["serverSeq"]
    assert _seq(client, "echo:/cm-b", "session/chatAdded") < envelope["serverSeq"]
    # The values move unchanged, and the reducer's clear of provider/schema is a no-op here.
    state = host.sequencer.state_of(b) or {}
    assert state["config"] == {"values": {"prefix": "You said:"}}
    assert "provider" not in state


async def test_the_descendants_are_told_too(host: Host) -> None:
    client = await _client(host)
    await _session(host, client, "echo:/cm-c")
    await _session(host, client, "echo:/cm-d")
    b, side = "ahp-chat:/cm-c1", "ahp-chat:/cm-c-side"
    await _chat(client, "echo:/cm-c", b)
    await _turn(host, client, b)
    await _chat(
        client,
        "echo:/cm-c",
        side,
        source={"kind": "sideChat", "chat": b, "turnId": "t1"},
    )
    await client.request("subscribe", {"channel": b})
    await client.request("subscribe", {"channel": side})

    await _move(client, b, {"kind": "session", "session": "echo:/cm-d"})
    await client.collect_until(lambda: bool(_moved(client, b)) and bool(_moved(client, side)))

    assert [e["action"]["session"] for e in _moved(client, b)] == ["echo:/cm-d"]
    assert [e["action"]["session"] for e in _moved(client, side)] == ["echo:/cm-d"]


async def test_a_new_session_move_names_the_allocated_session(host: Host) -> None:
    client = await _client(host)
    await _session(host, client, "echo:/cm-e")
    b = "ahp-chat:/cm-e1"
    await _chat(client, "echo:/cm-e", b)
    await client.request("subscribe", {"channel": b})

    moved = await _move(client, b, {"kind": "newSession"})
    created = moved["result"]["session"]
    await client.collect_until(lambda: bool(_moved(client, b)))

    assert [e["action"] for e in _moved(client, b)] == [{"type": "chat/moved", "session": created}]


async def test_a_reorder_or_a_refused_move_dispatches_nothing(host: Host) -> None:
    client = await _client(host)
    default = await _session(host, client, "echo:/cm-f")
    b = "ahp-chat:/cm-f1"
    await _chat(client, "echo:/cm-f", b)
    await client.request("subscribe", {"channel": b})

    await _move(client, b, {"kind": "session", "session": "echo:/cm-f"})
    refused = await _move(client, default, {"kind": "session", "session": "echo:/cm-f"})
    assert "error" in refused
    await client.collect(seconds=0.2)

    assert _moved(client, b) == []


def test_the_reducer_leaves_a_native_chat_without_provider_or_schema() -> None:
    state = {"resource": "c", "config": {"values": {"a": 1}}, "turns": []}
    after = chat_reducer(state, {"type": "chat/moved", "session": "s"})
    assert after["config"] == {"values": {"a": 1}}
    assert "provider" not in after


async def test_a_move_between_sessions_of_different_agents_is_refused() -> None:
    """A moved chat keeps its agent and a session's chats are its agent's: a move to a
    session of another provider is refused as an unsupported destination, nothing
    published and nothing changed."""
    other = EchoProvider(provider_id="second", capabilities=_CHATS)
    host = Host(
        [EchoProvider(capabilities=_CHATS), other],
        LoopbackSingleUserPolicy(),
    )
    try:
        client = await _client(host)
        await client.request("createSession", {"channel": "echo:/cm-g", "provider": "echo"})
        await client.request("createSession", {"channel": "second:/cm-h", "provider": "second"})
        await client.collect_until(
            lambda: all(
                (host.sequencer.state_of(u) or {}).get("lifecycle") == "ready"
                for u in ("echo:/cm-g", "second:/cm-h")
            )
        )
        b = "ahp-chat:/cm-g1"
        await _chat(client, "echo:/cm-g", b)
        await client.request("subscribe", {"channel": b})

        refused = await _move(client, b, {"kind": "session", "session": "second:/cm-h"})

        assert refused["error"]["code"] == -32009
        await client.collect(seconds=0.2)
        assert _moved(client, b) == []
        assert b in host._sessions["echo:/cm-g"].chat_uris
    finally:
        await host.aclose()
