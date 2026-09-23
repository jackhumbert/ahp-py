"""Router internals the end-to-end tests cannot pin down: races and faults.

These drive `_SurfaceConnection` against a scripted node link, because the
interleavings under test (two subscribes of one channel, a frame landing
mid-handshake, a node that fails to close) cannot be produced on demand by a
real host.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from agent_host_protocol import AhpError, memory_pair

from agent_host_broker.core.broker import Broker, _Node, _SurfaceConnection
from agent_host_broker.core.node import NodeRequestHandler
from agent_host_broker.core.paging import has_more, merge_pages
from agent_host_broker.core.sequence import LinkSequence
from agent_host_broker.registry import StaticInventory


class ScriptedLink:
    """A node link whose `subscribe` replies only when the test says so."""

    def __init__(self, node_id: str = "n", fail_close: bool = False) -> None:
        self._node_id = node_id
        self.replies: asyncio.Queue[asyncio.Future[Any]] = asyncio.Queue()
        self.fail_close = fail_close
        self.closed = False
        self.notified: list[tuple[str, Mapping[str, Any]]] = []

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def handshake(self) -> Mapping[str, Any]:
        return {}

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        reply: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self.replies.put(reply)
        return await reply

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self.notified.append((method, dict(params)))

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        return
        yield {}

    def set_request_handler(self, handler: NodeRequestHandler | None) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True
        if self.fail_close:
            raise OSError("socket already gone")


class Unused:
    async def connect(self, record: Any, principal: Any) -> Any:
        raise AssertionError("not dialed in these tests")


def connection() -> _SurfaceConnection:
    broker = Broker(StaticInventory([]), Unused(), lambda info: None)
    _, broker_end = memory_pair()
    conn = _SurfaceConnection(broker, broker_end, peer=None, headers=None, token=None)
    conn.initialized = True
    return conn


def attach(conn: _SurfaceConnection, link: ScriptedLink, channels: set[str]) -> _Node:
    node = _Node(link=link, sequence=LinkSequence(conn.clock, 0), root={})
    conn.nodes[link.node_id] = node
    conn.owners.claim(link.node_id, channels)
    return node


def action(channel: str, seq: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "action",
        "params": {"channel": channel, "action": {"type": "x"}, "serverSeq": seq},
    }


def drain(conn: _SurfaceConnection) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    while not conn.outbox.empty():
        message = conn.outbox.get_nowait()
        assert message is not None
        sent.append(message)
    return sent


def snapshot(channel: str, from_seq: int) -> dict[str, Any]:
    return {"snapshot": {"resource": channel, "state": {"turns": []}, "fromSeq": from_seq}}


async def test_overlapping_subscribes_of_one_channel_lose_and_repeat_nothing() -> None:
    conn = connection()
    link = ScriptedLink()
    node = attach(conn, link, {"ch"})
    after_1: list[dict[str, Any]] = []
    after_2: list[dict[str, Any]] = []
    first = asyncio.create_task(conn._subscribe_channel("ch", after_1))
    reply_1 = await link.replies.get()
    second = asyncio.create_task(conn._subscribe_channel("ch", after_2))
    reply_2 = await link.replies.get()

    conn._relay(node, action("ch", 1))
    assert drain(conn) == [], "nothing may go live while a subscribe is in flight"

    reply_1.set_result(snapshot("ch", 0))
    await first
    conn._relay(node, action("ch", 2))
    assert drain(conn) == [], "the second subscribe is still in flight"

    reply_2.set_result(snapshot("ch", 0))
    await second
    assert [f["params"]["serverSeq"] for f in after_1] == [1]
    # Seq 1 was already released behind the first reply: only seq 2 is new.
    assert [f["params"]["serverSeq"] for f in after_2] == [2]

    conn._relay(node, action("ch", 3))
    assert [f["params"]["serverSeq"] for f in drain(conn)] == [3]


async def test_frames_sent_during_initialize_follow_its_reply() -> None:
    conn = connection()
    conn.initialized = False

    async def dispatch(method: str, params: Any, after: list[dict[str, Any]]) -> Any:
        conn._send({"jsonrpc": "2.0", "method": "action", "params": {"serverSeq": 9}})
        return {"serverSeq": 9}

    conn._dispatch = dispatch  # type: ignore[method-assign]
    await conn._handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    sent = drain(conn)
    assert sent[0]["id"] == 1
    assert sent[1]["method"] == "action"
    assert conn.held is None


async def test_a_channel_whose_node_left_is_refused_not_rerouted() -> None:
    conn = connection()
    attach(conn, ScriptedLink("still-here"), set())
    conn.owners.claim("gone", {"gone:/session"})
    with pytest.raises(AhpError, match="not connected"):
        conn._route({"channel": "gone:/session"})


async def test_one_node_failing_to_close_does_not_skip_the_rest() -> None:
    surface_end, broker_end = memory_pair()
    broker = Broker(StaticInventory([]), Unused(), lambda info: None)
    conn = _SurfaceConnection(broker, broker_end, peer=None, headers=None, token=None)
    bad, good = ScriptedLink("bad", fail_close=True), ScriptedLink("good")
    attach(conn, bad, set())
    attach(conn, good, set())
    await surface_end.close()
    await conn.run()
    assert bad.closed
    assert good.closed


async def test_a_node_repeating_an_empty_cursor_ends_its_own_listing() -> None:
    async def fetch(node: str, cursor: str | None, limit: int) -> Mapping[str, Any]:
        if node == "stuck":
            return {"items": [], "nextCursor": "same"}
        return {"items": [{"resource": "ok:/1", "modifiedAt": "1"}]}

    page, positions = await merge_pages(["stuck", "ok"], {}, 10, fetch)
    assert [item["resource"] for item in page] == ["ok:/1"]
    assert not has_more(positions)


async def test_what_the_owner_streams_while_being_probed_is_not_lost() -> None:
    # A reconnect's subscribe for a channel no node has named yet: the broker
    # asks each node in turn. An action the owner streams while the probe is
    # still asking must reach the surface behind the snapshot, not vanish.
    conn = connection()
    owner, other = ScriptedLink("a"), ScriptedLink("b")
    node_a = attach(conn, owner, set())
    attach(conn, other, set())
    after: list[dict[str, Any]] = []
    probing = asyncio.create_task(conn._subscribe_channel("ch", after))

    reply_a = await owner.replies.get()
    reply_a.set_result(snapshot("ch", 0))
    reply_b = await other.replies.get()
    # The owner's subscription is live now, and it streams before the probe
    # has heard from every node.
    conn._relay(node_a, action("ch", 1))
    assert drain(conn) == [], "held until the reply, not sent ahead of it"
    reply_b.set_result({})
    result = await probing

    assert result is not None
    assert result["resource"] == "ch"
    assert [f["params"]["serverSeq"] for f in after] == [1]
    assert conn.owners.owner_of("ch") == "a"
    # Only the node that does not have the channel is unsubscribed.
    assert owner.notified == []
    assert other.notified == [("unsubscribe", {"channel": "ch"})]
