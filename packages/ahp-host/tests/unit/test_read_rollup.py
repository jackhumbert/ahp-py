"""A session is read when every one of its chats is read.

Some clients mark a chat read (`chat/isReadChanged`) and never send
`session/isReadChanged`, which left the session's unread dot up for good. The
host rolls the chat bits up into the session bit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.types.protocol import SessionStatus

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.store import FileSessionStore

from .hosting import Wire, connect, open_session, run_turn, shut, state
from .test_hosts_chats import HostingProvider, _chat

pytestmark = pytest.mark.anyio

_READ = int(SessionStatus.IS_READ)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _is_read(host: Host, uri: str) -> bool:
    status = state(host, uri).get("status")
    return isinstance(status, int) and bool(status & _READ)


def _read_actions(wire: Wire, uri: str) -> list[dict[str, Any]]:
    return wire.actions(uri, "session/isReadChanged")


async def _unread_after_turn(wire: Wire, uri: str, chat: str, turn: str) -> None:
    """Read the session and chat, run a turn, and wait for both to turn unread."""
    await wire.dispatch(uri, {"type": "session/isReadChanged", "isRead": True})
    await wire.dispatch(chat, {"type": "chat/isReadChanged", "isRead": True})
    await run_turn(wire, chat, turn)
    assert await wire.until(lambda: not _is_read(wire.host, uri))
    assert not _is_read(wire.host, chat)


async def test_reading_the_only_chat_reads_the_session() -> None:
    host = Host(HostingProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    uri = "echo:/rr-1"
    try:
        chat = await open_session(wire, uri)
        await _unread_after_turn(wire, uri, chat, "t1")
        before = len(_read_actions(wire, uri))
        await wire.dispatch(chat, {"type": "chat/isReadChanged", "isRead": True})
        assert await wire.until(lambda: _is_read(host, uri))
        assert _is_read(host, chat)
        added = _read_actions(wire, uri)[before:]
        assert [a["isRead"] for a in added] == [True]
    finally:
        await shut(host, wire, serving)


async def test_the_session_is_read_only_once_every_chat_is() -> None:
    host = Host(HostingProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    uri = "echo:/rr-2"
    try:
        first = await open_session(wire, uri)
        second = "ahp-chat:/second"
        await _chat(wire, uri, second)
        await _unread_after_turn(wire, uri, first, "t1")
        await run_turn(wire, second, "t2")
        assert not _is_read(host, second)
        await wire.dispatch(first, {"type": "chat/isReadChanged", "isRead": True})
        assert await wire.until(lambda: _is_read(host, first))
        await wire.dispatch(uri, {"type": "session/titleChanged", "title": "sync"})
        assert await wire.until(lambda: state(host, uri).get("title") == "sync")
        assert not _is_read(host, uri)
        await wire.dispatch(second, {"type": "chat/isReadChanged", "isRead": True})
        assert await wire.until(lambda: _is_read(host, uri))
    finally:
        await shut(host, wire, serving)


async def test_an_explicit_session_read_still_works() -> None:
    host = Host(HostingProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    uri = "echo:/rr-3"
    try:
        chat = await open_session(wire, uri)
        await _unread_after_turn(wire, uri, chat, "t1")
        await wire.dispatch(uri, {"type": "session/isReadChanged", "isRead": True})
        assert await wire.until(lambda: _is_read(host, uri))
        assert not _is_read(host, chat)
    finally:
        await shut(host, wire, serving)


async def test_the_rolled_up_bit_is_persisted(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path)
    host = Host(HostingProvider(), LoopbackSingleUserPolicy(), store=store)
    wire, serving = await connect(host)
    uri = "echo:/rr-4"
    try:
        chat = await open_session(wire, uri)
        await _unread_after_turn(wire, uri, chat, "t1")
        await wire.dispatch(chat, {"type": "chat/isReadChanged", "isRead": True})
        assert await wire.until(lambda: _is_read(host, uri))
    finally:
        await shut(host, wire, serving)

    [stored] = await FileSessionStore(tmp_path).load_all()
    status = stored.channels[uri]["status"]
    assert status & _READ
