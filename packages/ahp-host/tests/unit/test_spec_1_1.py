"""What the 1.1.0 absorb asks of the host beyond the reducers.

The pending list's reader, the `createChat` title, and the one experimental
command this host declines. The pending-message and per-chat config flows are
driven end to end in `tests/integration/`.
"""

from __future__ import annotations

from typing import Any

import pytest
from ahp_protocol.types import AHP_ERROR_CODES

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.host import _pending_entries
from ahp_host.provider import EchoProvider

from .hosting import connect, open_session, shut, state

pytestmark = pytest.mark.anyio

_MESSAGE = {"text": "m", "origin": {"kind": "user"}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _entry(identifier: str, *kind: str | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": identifier, "message": _MESSAGE}
    if kind:
        entry["kind"] = kind[0]
    return entry


class TestPendingEntries:
    def test_a_kindless_entry_is_queued(self) -> None:
        """Absence "means 'queued' for entries read from old queued-message
        snapshots" -- and an explicit null reads the same way."""
        entries = _pending_entries({"queuedMessages": [_entry("a"), _entry("b", None)]})
        assert [(kind, e["id"]) for kind, e in entries] == [("queued", "a"), ("queued", "b")]

    def test_raw_order_is_kept_across_kinds(self) -> None:
        listed = [_entry("q1", "queued"), _entry("s1", "steering"), _entry("q2")]
        entries = _pending_entries({"queuedMessages": listed})
        assert [(kind, e["id"]) for kind, e in entries] == [
            ("queued", "q1"),
            ("steering", "s1"),
            ("queued", "q2"),
        ]

    def test_the_mirror_is_deduplicated_against_the_list(self) -> None:
        """ "using the list entry and its kind when present" -- a mirror whose
        id is listed adds nothing, even under a different kind."""
        state = {
            "steeringMessage": _entry("x"),
            "queuedMessages": [_entry("x", "queued")],
        }
        assert [(kind, e["id"]) for kind, e in _pending_entries(state)] == [("queued", "x")]

    def test_a_mirror_only_entry_is_steering_and_comes_first(self) -> None:
        """A session saved before 1.1.0 holds its steering message only in the
        singleton, kind and all absent: "A singleton-only entry from an old
        snapshot remains steering input even if its `kind` is absent." """
        state = {"steeringMessage": _entry("old"), "queuedMessages": [_entry("q1")]}
        assert [(kind, e["id"]) for kind, e in _pending_entries(state)] == [
            ("steering", "old"),
            ("queued", "q1"),
        ]

    def test_an_unknown_kind_is_left_alone(self) -> None:
        entries = _pending_entries({"queuedMessages": [_entry("z", "someday"), _entry("q")]})
        assert [(kind, e["id"]) for kind, e in entries] == [("queued", "q")]

    def test_garbage_reads_as_nothing(self) -> None:
        assert _pending_entries(None) == []
        assert _pending_entries({"queuedMessages": "nope", "steeringMessage": 3}) == []


class TestCreateChatTitle:
    async def test_the_clients_title_names_the_chat(self) -> None:
        """`CreateChatParams.title`: "a client-selected initial display title"."""
        host = Host(EchoProvider(capabilities={"multipleChats": {}}), LoopbackSingleUserPolicy())
        wire, serving = await connect(host)
        try:
            session = "echo:/titled"
            await open_session(wire, session)
            await wire.request(
                "createChat",
                {"channel": session, "chat": "ahp-chat:/titled-b", "title": "Startup timing"},
            )
            assert state(host, "ahp-chat:/titled-b")["title"] == "Startup timing"
            chats = state(host, session)["chats"]
            assert any(c["title"] == "Startup timing" for c in chats), chats
        finally:
            await shut(host, wire, serving)

    async def test_a_blank_title_falls_back(self) -> None:
        host = Host(EchoProvider(capabilities={"multipleChats": {}}), LoopbackSingleUserPolicy())
        wire, serving = await connect(host)
        try:
            session = "echo:/untitled"
            await open_session(wire, session)
            await wire.request(
                "createChat", {"channel": session, "chat": "ahp-chat:/untitled-b", "title": " "}
            )
            assert state(host, "ahp-chat:/untitled-b")["title"] == "New Chat"
        finally:
            await shut(host, wire, serving)


async def test_create_tcp_connection_is_refused_by_name() -> None:
    """Experimental, and only for a host advertising `tcpConnections`. Refused
    with a reason rather than `MethodNotFound`, which is for methods the
    protocol does not define (invariant 14)."""
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        response = await wire.request(
            "createTcpConnection",
            {"channel": "ahp-tcp:/t1", "host": "localhost", "port": 3000},
        )
        assert response["error"]["code"] == AHP_ERROR_CODES["PermissionDenied"]
    finally:
        await shut(host, wire, serving)


async def test_a_windowed_subscribe_gets_ordinary_delivery() -> None:
    """ "A server that does not support windowed delivery, or that declines the
    offer, omits `flowControl` and returns an ordinary result." """
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/windowed")
        response = await wire.request(
            "subscribe",
            {
                "channel": "echo:/windowed",
                "flowControl": {"receive": {"windowBytes": 262144, "maximumFrameBytes": 16384}},
            },
        )
        assert "flowControl" not in response["result"]
        assert response["result"]["snapshot"]["resource"] == "echo:/windowed"
    finally:
        await shut(host, wire, serving)
