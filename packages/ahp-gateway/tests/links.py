"""A recording node link, for router behaviour a real host cannot be made to show.

`tests.fleet` drives real hosts, and is the evidence that stock peers accept
the gateway. This is the other kind of test: what exactly the gateway sent a
node, for shapes the sibling host does not produce (a `{level}` telemetry
template) or does not answer distinguishably (`subscribe` options it ignores).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from ahp_protocol import AhpError, memory_pair

from ahp_gateway.core.gateway import Gateway, _Node, _SurfaceConnection
from ahp_gateway.core.node import NodeRequestHandler
from ahp_gateway.core.sequence import LinkSequence
from ahp_gateway.core.telemetry import node_signals
from ahp_gateway.registry import NodeRecord, StaticInventory
from tests.fleet import DEV


class RecordingLink:
    """Answers every request from `replies` (default `{}`) and records it."""

    def __init__(
        self,
        node_id: str,
        handshake: Mapping[str, Any] | None = None,
        *,
        replies: Mapping[str, Any] | None = None,
        refuse: bool = False,
    ) -> None:
        self._node_id = node_id
        self._handshake = dict(handshake or {})
        self.replies = dict(replies or {})
        self.refuse = refuse
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notified: list[tuple[str, dict[str, Any]]] = []

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def handshake(self) -> Mapping[str, Any]:
        return self._handshake

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        self.requests.append((method, dict(params)))
        if self.refuse:
            raise AhpError(-32009, f"{self._node_id} will not")
        return self.replies.get(method, {})

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self.notified.append((method, dict(params)))

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        return
        yield {}

    def set_request_handler(self, handler: NodeRequestHandler | None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class Unused:
    async def connect(self, record: Any, principal: Any) -> Any:
        raise AssertionError("not dialed in these tests")


def connection(*links: RecordingLink) -> _SurfaceConnection:
    """An initialized surface connection whose nodes are `links`, in order."""
    gateway = Gateway(StaticInventory([]), Unused(), lambda info: None)
    _, gateway_end = memory_pair()
    conn = _SurfaceConnection(gateway, gateway_end, peer=None, headers=None, token=None)
    conn.initialized = True
    for link in links:
        conn.records[link.node_id] = NodeRecord(link.node_id, f"mem://{link.node_id}", DEV)
        conn.nodes[link.node_id] = _Node(
            link=link,
            sequence=LinkSequence(conn.clock, 0),
            root={},
            telemetry=node_signals(link.handshake),
        )
    return conn


def drain(conn: _SurfaceConnection) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    while not conn.outbox.empty():
        message = conn.outbox.get_nowait()
        assert message is not None
        sent.append(message)
    return sent
