"""The client edge: one AHP client connection from the gateway to one node.

The interface the router depends on is :class:`NodeLink` - raw JSON-RPC in
both directions, because the gateway relays frames rather than interpreting
them, and anything it interpreted it would have to re-encode losslessly. The
one implementation, :class:`AhpNodeLink`, is `ahp_client.AhpClient`
underneath: the handshake, request ids, timeouts and host-initiated requests
are the sibling's, not reimplemented here.

**Fidelity limits inherited from AhpClient**, both worth a sibling change:

* Notifications are rebuilt from its typed events, and it forwards only the
  methods it models (`NOTIFICATION_METHODS`). Today that is every notification
  the spec defines; a method added upstream would be dropped at the node edge
  until the client learns it.
* Its `events()` tap is bounded. A drop there means a surface has missed state
  it can never recover through this link, so a drop closes the link rather
  than letting the surface continue on a silently wrong mirror.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from ahp_client import AhpClient, AhpClientError, ClientConfig, RpcError
from ahp_client.client.events import (
    ActionEvent,
    AuthRequiredEvent,
    DroppedEvents,
    OtlpEvent,
    ProgressEvent,
    SessionAdded,
    SessionRemoved,
    SessionSummaryChanged,
)
from ahp_protocol import ROOT_URI, AhpError, Transport
from ahp_protocol.channels import AUTOMATIONS_URI

from ahp_gateway.registry import NodeRecord, Principal

__all__ = [
    "AhpNodeLink",
    "NodeConnector",
    "NodeLink",
    "NodeRequestHandler",
    "NodeUnavailableError",
    "open_node_link",
]

NodeRequestHandler = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
"""Answers a request the node sent. Raise :class:`AhpError` for an error reply."""


class NodeUnavailableError(AhpError):
    """The node could not be reached, or its link has closed."""

    def __init__(self, node_id: str, detail: str) -> None:
        super().__init__(-32603, f"node {node_id!r} is unavailable: {detail}")
        self.node_id = node_id


@runtime_checkable
class NodeLink(Protocol):
    @property
    def node_id(self) -> str: ...

    @property
    def handshake(self) -> Mapping[str, Any]:
        """The node's `initialize` result."""
        ...

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        """Forward a request; the node's error arrives as :class:`AhpError`."""
        ...

    def notify(self, method: str, params: Mapping[str, Any]) -> None: ...

    def frames(self) -> AsyncIterator[dict[str, Any]]:
        """Every notification the node sends, as a JSON-RPC frame. Ends on close."""
        ...

    def set_request_handler(self, handler: NodeRequestHandler | None) -> None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class NodeConnector(Protocol):
    """Opens the transport to a node, on behalf of one principal.

    The principal is passed because the node's host runs as that developer's
    own OS account (docs/plan.md §4): which host to reach, and with what
    credential, can depend on who is asking.
    """

    async def connect(self, record: NodeRecord, principal: Principal) -> Transport: ...


def _frame(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": dict(params)}


def _as_frame(event: object) -> dict[str, Any] | None:
    if isinstance(event, ActionEvent):
        return _frame("action", event.envelope)
    if isinstance(event, SessionAdded):
        return _frame("root/sessionAdded", event.params)
    if isinstance(event, SessionRemoved):
        return _frame("root/sessionRemoved", event.params)
    if isinstance(event, SessionSummaryChanged):
        return _frame("root/sessionSummaryChanged", event.params)
    if isinstance(event, AuthRequiredEvent):
        return _frame("auth/required", event.params)
    if isinstance(event, ProgressEvent):
        return _frame("root/progress", event.params)
    if isinstance(event, OtlpEvent):
        return _frame(f"otlp/export{event.signal.capitalize()}", event.params)
    return None


class AhpNodeLink:
    def __init__(self, node_id: str, client: AhpClient) -> None:
        self._node_id = node_id
        self._client = client
        # Readers are attached before the handshake, so nothing the node sends
        # in reply to `initialize` can slip past them.
        self._events = client.events()
        self._diagnostics = client.diagnostics()
        self._handshake: Mapping[str, Any] = {}
        self._handler: NodeRequestHandler | None = None
        self._closed = asyncio.Event()
        self._watchdog: asyncio.Task[None] | None = None
        client.set_server_request_handler(self._answer)

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def handshake(self) -> Mapping[str, Any]:
        return self._handshake

    async def start(
        self,
        *,
        client_id: str,
        protocol_version: str,
        client_info: Mapping[str, Any] | None = None,
        locale: str | None = None,
    ) -> None:
        await self._client.connect()
        self._watchdog = asyncio.create_task(self._watch_diagnostics())
        try:
            self._handshake = await self._client.initialize(
                client_id=client_id,
                protocol_versions=[protocol_version],
                # The gateway always holds each node's root: it is where the
                # fleet's agent list comes from, whether or not the surface
                # has subscribed to the merged root yet. Likewise its
                # automation catalogue; a node with none answers no snapshot
                # for it, which is how a host says it has no such channel.
                initial_subscriptions=[ROOT_URI, AUTOMATIONS_URI],
                client_info=client_info,
                # The surface's, so a node localises what it shows that surface
                # (confirmation option labels). Its `capabilities` are not
                # passed on: see docs/plan.md §9.
                locale=locale,
            )
        except AhpClientError as exc:
            await self.aclose()
            raise self._translate(exc) from exc

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        if self._closed.is_set():
            raise NodeUnavailableError(self._node_id, "link closed")
        try:
            return await self._client.request(method, params)
        except AhpClientError as exc:
            raise self._translate(exc) from exc

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        if self._closed.is_set():
            return
        with contextlib.suppress(AhpClientError):
            self._client.notify(method, params)

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        async for tagged in self._events:
            frame = _as_frame(tagged.event)
            if frame is not None:
                yield frame
        await self.aclose()

    def set_request_handler(self, handler: NodeRequestHandler | None) -> None:
        self._handler = handler

    async def aclose(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._watchdog is not None and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        await self._client.shutdown()
        await self._events.aclose()
        await self._diagnostics.aclose()

    async def _answer(self, method: str, params: Mapping[str, Any]) -> Any:
        if self._handler is None:
            raise RpcError(-32601, f'no handler for server method "{method}"')
        try:
            return await self._handler(method, params)
        except AhpError as exc:
            raise RpcError(exc.code, exc.message, exc.data) from exc

    async def _watch_diagnostics(self) -> None:
        async for diagnostic in self._diagnostics:
            if isinstance(diagnostic, DroppedEvents) and diagnostic.stream == "events":
                # See the module docstring: a surface behind a lossy link is
                # worse off than one whose node visibly went away.
                await self.aclose()
                return

    def _translate(self, exc: AhpClientError) -> AhpError:
        if isinstance(exc, RpcError):
            return AhpError(exc.code if exc.code is not None else -32603, exc.message, exc.data)
        return NodeUnavailableError(self._node_id, str(exc) or type(exc).__name__)


async def open_node_link(
    node_id: str,
    transport: Transport,
    *,
    client_id: str,
    protocol_version: str,
    event_buffer: int = 4096,
    client_info: Mapping[str, Any] | None = None,
    locale: str | None = None,
) -> AhpNodeLink:
    """Connect and handshake. The node sees the surface's own `clientId`.

    Passing the surface's id through, rather than minting a gateway id, is what
    keeps the node's view coherent without rewriting: `activeClient`, action
    `origin`s and claims all name the client the surface knows itself as.
    """
    config = ClientConfig(protocol_versions=(protocol_version,), event_buffer=event_buffer)
    link = AhpNodeLink(node_id, AhpClient(transport, config))
    await link.start(
        client_id=client_id,
        protocol_version=protocol_version,
        client_info=client_info,
        locale=locale,
    )
    return link
