"""The multiplexer: one AHP endpoint for the surfaces, one AHP link per node.

A surface connects to :meth:`Broker.serve` exactly as it would to a host. At
`initialize` the broker authenticates the peer, asks the registry which nodes
the principal may use, and opens one AHP client link to each, handshaking as
the surface's own `clientId`. From then on it relays:

* **Requests** route to the node that owns their target (a channel the node
  has named, a file URI's authority, or the one node offering a provider);
  `listSessions` fans out to every node and merges into one flat list.
* **Actions** from the nodes are restamped onto one broker `serverSeq`
  (`agent_host_broker.core.sequence`), and every `fromSeq` translated to match.
* **The root channel** is the one thing synthesized rather than relayed: it
  is merged from every node's (`agent_host_broker.core.root`).
* **Host-initiated requests** (a node reading a client-side resource, an
  elicitation) go back to the surface and their answer back to the node.

Everything else passes through verbatim except file URIs, which gain the node
as their authority (`agent_host_broker.core.uris`). No private method, header
or action type crosses either edge: broker<->node is plain AHP (invariant 3),
and a surface cannot tell the endpoint is not a single host (invariant 1).

**Reconnect and recovery.** `reconnect` is always answered with the snapshot
arm: the broker keeps no replay log, but it can re-dial every node and hand
back a fresh snapshot of every channel, which is exact. A node that drops (or
was down at connect time) is redialed in the background; when it answers, the
broker closes the surface connection on purpose, and the surface's own
reconnect brings everything - that node included - back from fresh snapshots.
AHP has no server-pushed re-snapshot, so this is the one way to repair a
surface's state without inventing wire semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from agent_host_protocol import (
    DEFAULT_SUPPORTED_VERSIONS,
    REDUCERS,
    ROOT_URI,
    AhpError,
    Transport,
    TransportClosed,
    negotiate,
)
from agent_host_protocol.errors import (
    internal_error,
    invalid_params,
    provider_not_found,
    unsupported_protocol_version,
)
from agent_host_server import ConnectionInfo

import agent_host_broker
from agent_host_broker.core.node import NodeConnector, NodeLink, open_node_link
from agent_host_broker.core.paging import (
    check_limit,
    decode_cursor,
    encode_cursor,
    has_more,
    merge_pages,
)
from agent_host_broker.core.root import merge_root, root_actions
from agent_host_broker.core.sequence import BrokerClock, LinkSequence
from agent_host_broker.core.uris import (
    VIRTUAL_ROOT,
    ChannelOwners,
    ForeignUriError,
    from_client_alias,
    is_virtual_root,
    learn_owned_channels,
    node_of,
    qualify_file_uris,
    root_of,
    unqualify_file_uris,
)
from agent_host_broker.registry import NodeDirectory, NodeRecord, Principal

__all__ = ["Authenticator", "Broker", "BrokerInfo"]

#: `RootState._meta` key listing the machines behind the broker (namespaced, as
#: the spec asks of `_meta` keys; clients that do not know it ignore it).
NODES_META_KEY = "agent-host-broker/nodes"

_log = logging.getLogger(__name__)

Authenticator = Callable[[ConnectionInfo], Principal | None]
"""Who the peer is, from what its handshake carried; ``None`` refuses it.

Typically reads a principal a reverse proxy asserted in a header after SSO.
Such a header is evidence only if the socket is reachable solely through
that proxy - the embedder's guarantee to make, as with `agent_host_server`.
"""

#: `-32009`, the code the sibling host uses for a policy refusal.
_REFUSED: Final = -32009

#: Params whose value is a channel some node owns, in routing precedence.
_CHANNEL_KEYS: Final = ("channel", "resource", "session", "sessionResource", "chat")
#: Params whose value may be a file URI naming its node.
_FILE_KEYS: Final = ("uri", "root", "cwd", "workingDirectory", "channel", "resource")
#: Asked before a session exists, often before its folder is chosen.
_SESSION_CONFIG_METHODS: Final = frozenset({"resolveSessionConfig", "sessionConfigCompletions"})


@dataclass(frozen=True)
class BrokerInfo:
    """What the surfaces are told they are talking to."""

    name: str = "agent-host-broker"
    version: str = agent_host_broker.__version__
    title: str | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.title is not None:
            wire["title"] = self.title
        return wire


class Broker:
    def __init__(
        self,
        directory: NodeDirectory,
        connector: NodeConnector,
        authenticate: Authenticator,
        *,
        info: BrokerInfo | None = None,
        supported_versions: Sequence[str] = DEFAULT_SUPPORTED_VERSIONS,
        connect_timeout: float = 10.0,
        redial_backoff: tuple[float, float] = (0.5, 30.0),
    ) -> None:
        self.directory = directory
        self.connector = connector
        self.authenticate = authenticate
        self.info = info or BrokerInfo()
        self.supported_versions = tuple(supported_versions)
        self.connect_timeout = connect_timeout
        #: (first delay, ceiling) in seconds, doubling, for redialing a node.
        self.redial_backoff = redial_backoff

    async def serve(
        self,
        transport: Transport,
        *,
        peer: str | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> None:
        """Drive one surface connection until its transport closes.

        The same signature as `agent_host_server.Host.serve`, so whatever
        serves a host - that package's WebSocket server included - can serve
        the broker.
        """
        connection = _SurfaceConnection(self, transport, peer=peer, headers=headers, token=token)
        await connection.run()


@dataclass
class _Node:
    link: NodeLink
    sequence: LinkSequence
    root: dict[str, Any]
    #: The node's own root directory (from its `defaultDirectory`), which
    #: `ahp-file:///<node>` names; None when it advertised none.
    path_root: str | None = None
    pump: asyncio.Task[None] | None = None
    subscribed: set[str] = field(default_factory=set)


class _SurfaceConnection:
    def __init__(
        self,
        broker: Broker,
        transport: Transport,
        *,
        peer: str | None,
        headers: Mapping[str, str] | None,
        token: str | None,
    ) -> None:
        self.broker = broker
        self.transport = transport
        self.peer = peer
        self.headers = headers
        self.token = token
        self.initialized = False
        self.closing = False
        self.nodes: dict[str, _Node] = {}
        #: Set by the handshake: who this connection is, and what it may use.
        self.principal: Principal | None = None
        self.client_id = ""
        self.version = ""
        self.client_info: Mapping[str, Any] | None = None
        self.records: dict[str, NodeRecord] = {}
        self.clock = BrokerClock()
        self.owners = ChannelOwners()
        self.root: dict[str, Any] = merge_root([])
        #: Channels the surface holds a subscription to.
        self.subscriptions: set[str] = set()
        #: Channels mid-subscribe: one buffer per in-flight subscribe, holding
        #: the stamped frames that beat that request's snapshot home. While any
        #: buffer is open for a channel, nothing for it is sent live.
        self.pending: dict[str, list[list[tuple[int, dict[str, Any]]]]] = {}
        #: Per channel, the highest stamp already delivered to the surface, so
        #: two overlapping subscribes never release the same frame twice.
        self.delivered: dict[str, int] = {}
        #: While `initialize` is in flight, frames that must follow its reply.
        self.held: list[dict[str, Any]] | None = None
        self.outbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.outbound: dict[int, asyncio.Future[Any]] = {}
        self.next_outbound_id = 1
        self.tasks: set[asyncio.Task[None]] = set()

    # ─── the connection loop ─────────────────────────────────────────────

    async def run(self) -> None:
        writer = asyncio.create_task(self._write_loop())
        try:
            while True:
                try:
                    message = await self.transport.receive()
                except TransportClosed:
                    return
                if message is None:
                    return
                if "method" not in message:
                    self._resolve_outbound(message)
                elif "id" in message:
                    self._spawn(self._handle_request(message))
                else:
                    try:
                        self._handle_notification(message)
                    except Exception:
                        _log.exception("surface notification failed: %s", message.get("method"))
        finally:
            # Detached first, so the pumps this cancels do not report their
            # own ends as nodes dropping out of a fleet that is shutting down.
            self.closing = True
            nodes, self.nodes = list(self.nodes.values()), {}
            for task in list(self.tasks):
                task.cancel()
            for future in self.outbound.values():
                if not future.done():
                    future.set_exception(internal_error("surface disconnected"))
            # Together and tolerant: one node failing to close must not leave
            # the others open, the writer blocked, or the surface unclosed.
            results = await asyncio.gather(
                *(node.link.aclose() for node in nodes), return_exceptions=True
            )
            for node, result in zip(nodes, results, strict=True):
                if isinstance(result, BaseException):
                    _log.warning("closing node %s failed: %r", node.link.node_id, result)
            self.outbox.put_nowait(None)
            with contextlib.suppress(Exception):
                await writer
            with contextlib.suppress(Exception):
                await self.transport.close()

    async def _write_loop(self) -> None:
        while True:
            message = await self.outbox.get()
            if message is None:
                return
            try:
                await self.transport.send(message)
            except (TransportClosed, OSError):
                return

    def _spawn(self, coroutine: Any) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _send(self, message: dict[str, Any]) -> None:
        if self.held is not None:
            self.held.append(message)
        else:
            self.outbox.put_nowait(message)

    # ─── surface requests ────────────────────────────────────────────────

    async def _handle_request(self, message: Mapping[str, Any]) -> None:
        request_id = message["id"]
        method = str(message["method"])
        raw = message.get("params")
        params: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        # Frames that must reach the surface AFTER this reply: actions that
        # arrived for a channel while its snapshot was in flight.
        after: list[dict[str, Any]] = []
        # `initialize` subscribes to several channels and awaits between them,
        # so a channel it has finished could stream actions ahead of the reply
        # that carries its snapshot. Everything sent meanwhile is held, and
        # released behind the reply.
        holding = (
            method in {"initialize", "reconnect"} and self.held is None and not self.initialized
        )
        if holding:
            self.held = []
        reply: dict[str, Any]
        try:
            result = await self._dispatch(method, params, after)
            reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except AhpError as exc:
            reply = {"jsonrpc": "2.0", "id": request_id, "error": exc.to_json()}
        except Exception as exc:
            _log.exception("request %s failed", method)
            error = internal_error(f"{type(exc).__name__}: {exc}")
            reply = {"jsonrpc": "2.0", "id": request_id, "error": error.to_json()}
        released = after
        if holding:
            released = [*after, *(self.held or [])]
            self.held = None
        self.outbox.put_nowait(reply)
        for frame in released:
            self._send(frame)

    async def _dispatch(
        self, method: str, params: Mapping[str, Any], after: list[dict[str, Any]]
    ) -> Any:
        if method == "ping":
            return None
        if method == "initialize":
            return await self._initialize(params, after)
        if method == "reconnect":
            return await self._reconnect(params, after)
        if not self.initialized:
            raise invalid_params("initialize must be the first request")
        params = self._from_client(method, params)
        if method == "subscribe":
            return await self._subscribe(params, after)
        if method == "listSessions":
            return await self._list_sessions(params)
        if method == "createSession":
            return await self._create_session(params)
        if method.startswith("resource") and is_virtual_root(params.get("uri")):
            return self._virtual_root(method)
        if method == "authenticate":
            return await self._authenticate(params)
        node_id = self._route(params)
        if node_id is None and method in _SESSION_CONFIG_METHODS:
            # Asked before a folder is chosen: the default node answers, and
            # the client asks again once the folder names one.
            node_id = self._default_node(params.get("provider"))
        if node_id is None:
            # Nothing in the request names a node, and more than one could
            # answer it. Guessing would run the command somewhere the surface
            # did not mean.
            raise invalid_params(f"cannot tell which node {method} is for")
        return await self._call(node_id, method, params)

    def _from_client(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """Read a client's `file:///<node>/...` (how VS Code browses the tree) as `ahp-file`."""
        nodes = set(self.records)
        aliased = from_client_alias(params, nodes)
        if method.startswith("resource") and isinstance(aliased, Mapping):
            uri = params.get("uri")
            aliased = {**aliased, "uri": from_client_alias(uri, nodes, root_uri=True)}
        return aliased if isinstance(aliased, Mapping) else params

    def _virtual_root(self, method: str) -> Any:
        """`ahp-file:///`: a read-only directory with one entry per node."""
        if method == "resourceList":
            return {
                "entries": [
                    {"name": node_id, "type": "directory"}
                    for node_id in self.records
                    if node_id in self.nodes
                ]
            }
        if method == "resourceResolve":
            return {"uri": VIRTUAL_ROOT, "type": "directory"}
        raise AhpError(-32009, "the list of nodes is read-only")

    def _default_node(self, provider: Any) -> str | None:
        """The first connected node, in inventory order, offering `provider`.

        Where a request cannot say which node it is for - a new chat with no
        folder, or its settings asked for before one is picked - it goes here.
        """
        offering = set(self._nodes_offering(provider)) if isinstance(provider, str) else set()
        for node_id in self.records:
            if node_id in self.nodes and (not offering or node_id in offering):
                return node_id
        return None

    async def _initialize(self, params: Mapping[str, Any], after: list[dict[str, Any]]) -> Any:
        if self.initialized:
            raise invalid_params("already initialized")
        offered = params.get("protocolVersions")
        if (
            not isinstance(offered, list)
            or not offered
            or not all(isinstance(version, str) for version in offered)
        ):
            raise invalid_params("protocolVersions must be a non-empty array of strings")
        chosen = negotiate(offered, self.broker.supported_versions)
        if chosen is None:
            raise unsupported_protocol_version(self.broker.supported_versions)

        client_info = params.get("clientInfo")
        await self._admit(
            params.get("clientId"),
            chosen,
            client_info if isinstance(client_info, Mapping) else None,
        )

        snapshots: list[dict[str, Any]] = []
        for uri in params.get("initialSubscriptions") or []:
            if not isinstance(uri, str) or uri == ROOT_URI:
                continue
            with contextlib.suppress(AhpError):
                snapshot = await self._subscribe_channel(uri, after)
                if snapshot is not None:
                    snapshots.append(snapshot)
        # Root last and with no await after it: the snapshot, the subscription
        # and the reply are one step, so no merged root action can be stamped
        # between the `fromSeq` taken here and the reply that carries it.
        if ROOT_URI in (params.get("initialSubscriptions") or []):
            snapshots.insert(0, self._subscribe_root())

        result: dict[str, Any] = {
            "protocolVersion": chosen,
            "serverSeq": self.clock.current,
            "serverInfo": self.broker.info.to_wire(),
            "snapshots": snapshots,
        }
        result.update(self._agreed_handshake_fields())
        return result

    async def _admit(
        self, client_id: Any, version: str, client_info: Mapping[str, Any] | None
    ) -> None:
        """Authenticate, then open a link to every node the principal may use.

        Shared by both handshakes: a `reconnect` is a new connection, and it is
        authenticated and admitted from scratch - the `clientId` it asserts is
        an identifier, never a credential.
        """
        if not isinstance(client_id, str) or not client_id:
            client_id = f"broker-{uuid.uuid4()}"
        info = ConnectionInfo(client_id, peer=self.peer, token=self.token, headers=self.headers)
        principal = self.broker.authenticate(info)
        if principal is None:
            raise AhpError(_REFUSED, "Connection refused by policy")
        self.principal, self.client_id, self.version = principal, client_id, version
        self.client_info = client_info

        # Admission before AHP (invariant 2): only nodes the registry admits
        # this principal to are ever dialed, so the rest cannot even appear.
        self.records = {r.node_id: r for r in self.broker.directory.nodes_for(principal)}
        opened = await asyncio.gather(*(self._open(r) for r in self.records.values()))
        for node in opened:
            if node is not None:
                self.nodes[node.link.node_id] = node
        self.root = merge_root([node.root for node in self.nodes.values()])
        self.initialized = True
        for node in self.nodes.values():
            node.pump = self._spawn(self._pump(node))
        for node_id in self.records:
            if node_id not in self.nodes:
                self._spawn(self._redial(node_id))

    async def _reconnect(self, params: Mapping[str, Any], after: list[dict[str, Any]]) -> Any:
        """Always the snapshot arm: fresh state for every channel still there.

        There is no replay log to answer the replay arm from, and none is
        needed - every node can be re-read. A channel no node can produce is
        simply absent, which is how the snapshot arm says it is gone.
        """
        if self.initialized:
            raise invalid_params("already initialized")
        last_seen = params.get("lastSeenServerSeq")
        if isinstance(last_seen, int) and not isinstance(last_seen, bool) and last_seen > 0:
            self.clock = BrokerClock(last_seen)
        # No `protocolVersions` to negotiate: adopt the most preferred, as the
        # sibling host does.
        await self._admit(params.get("clientId"), self.broker.supported_versions[0], None)

        wanted = [uri for uri in params.get("subscriptions") or [] if isinstance(uri, str)]
        snapshots: list[dict[str, Any]] = []
        for uri in dict.fromkeys(wanted):
            if uri == ROOT_URI:
                continue
            with contextlib.suppress(AhpError):
                snapshot = await self._subscribe_channel(uri, after)
                if snapshot is not None:
                    snapshots.append(snapshot)
        if ROOT_URI in wanted:
            snapshots.insert(0, self._subscribe_root())
        return {"type": "snapshot", "snapshots": snapshots}

    async def _redial(self, node_id: str) -> None:
        """Wait for a lost node to answer again, then bounce the surface.

        The link opened here is only proof of life and is closed at once: the
        bounce tears this connection down, and the surface's reconnect opens
        fresh links to every node and re-reads every channel from them.
        """
        record = self.records[node_id]
        delay, ceiling = self.broker.redial_backoff
        while not self.closing:
            await asyncio.sleep(delay)
            if self.closing:
                return
            node = await self._open(record, quiet=True)
            if node is not None:
                try:
                    await node.link.aclose()
                except Exception as exc:
                    # Still bounce: the surface's resync matters more than one
                    # probe link. But say so, since the node may now hold a
                    # connection nobody will close.
                    _log.warning("closing the probe link to %s failed: %r", node_id, exc)
                _log.info("node %s is back; closing the surface so it resyncs", node_id)
                self.closing = True
                with contextlib.suppress(Exception):
                    await self.transport.close()
                return
            delay = min(delay * 2, ceiling)

    async def _open(self, record: NodeRecord, *, quiet: bool = False) -> _Node | None:
        principal = self.principal
        assert principal is not None, "admission sets the principal before any dial"

        async def connect() -> NodeLink:
            transport = await self.broker.connector.connect(record, principal)
            return await open_node_link(
                record.node_id,
                transport,
                client_id=self.client_id,
                protocol_version=self.version,
                client_info=self.client_info,
            )

        try:
            link = await asyncio.wait_for(connect(), self.broker.connect_timeout)
        except Exception as exc:
            # One unreachable node degrades the fleet; it does not refuse the
            # surface. It is simply absent, as a node the principal was never
            # admitted to would be, until a redial finds it again.
            (_log.debug if quiet else _log.warning)("node %s unavailable: %s", record.node_id, exc)
            return None
        handshake = link.handshake
        node_seq = handshake.get("serverSeq")
        sequence = LinkSequence(self.clock, node_seq if isinstance(node_seq, int) else 0)
        root: dict[str, Any] = {}
        path_root = root_of(handshake.get("defaultDirectory"))
        for snapshot in handshake.get("snapshots") or []:
            if isinstance(snapshot, Mapping) and snapshot.get("resource") == ROOT_URI:
                state = qualify_file_uris(snapshot.get("state"), record.node_id, path_root)
                root = state if isinstance(state, dict) else {}
        self.owners.claim(record.node_id, learn_owned_channels(root))
        node_id = record.node_id

        async def answer(method: str, params: Mapping[str, Any]) -> Any:
            return await self._node_request(node_id, method, params)

        link.set_request_handler(answer)
        return _Node(link=link, sequence=sequence, root=root, path_root=path_root)

    def _agreed_handshake_fields(self) -> dict[str, Any]:
        """Handshake extras the whole fleet agrees on, and only those.

        A field one node advertises and another does not would promise the
        surface something that works on some sessions and not others -
        advertising what the fleet cannot do (invariant 4).
        """
        handshakes = [node.link.handshake for node in self.nodes.values()]
        agreed: dict[str, Any] = {}
        if not handshakes:
            return agreed
        for key in ("completionTriggerCharacters", "terminalCommandPrefix"):
            values = [handshake.get(key) for handshake in handshakes]
            if values[0] is not None and all(value == values[0] for value in values):
                agreed[key] = values[0]
        if len(self.nodes) == 1:
            # One node: its root, directly.
            ((node_id, node),) = self.nodes.items()
            directory = node.link.handshake.get("defaultDirectory")
            if isinstance(directory, str):
                agreed["defaultDirectory"] = qualify_file_uris(directory, node_id, node.path_root)
        else:
            # Several: the list of nodes, each of which is its own root.
            agreed["defaultDirectory"] = VIRTUAL_ROOT
        return agreed

    def _subscribe_root(self) -> dict[str, Any]:
        self.subscriptions.add(ROOT_URI)
        state = {**self.root, "_meta": {NODES_META_KEY: self._nodes_meta()}}
        return {"resource": ROOT_URI, "state": state, "fromSeq": self.clock.current}

    def _nodes_meta(self) -> list[dict[str, Any]]:
        """The machines behind this connection, for `RootState._meta`.

        AHP has no place for "which machine": a broker is one host to its
        surfaces (invariant 1), and a stock client needs nothing more - an
        agent offered on several machines is one agent, and the folder picks
        the machine. A client that wants to say "Claude on studio", group
        sessions by machine or offer only the agents a folder's machine runs
        reads this list; `RootState._meta` is the spec's place for metadata
        about the host itself, and clients ignore keys they do not know.

        A snapshot only: there is no root action for `_meta`. That holds
        because the list is fixed for a connection's life - admission decides
        it - and a node coming back closes the connection so it resyncs.
        """
        listed: list[dict[str, Any]] = []
        for node_id, record in self.records.items():
            node = self.nodes.get(node_id)
            entry: dict[str, Any] = {
                "id": node_id,
                "label": str(record.metadata.get("label") or node_id),
                "folder": f"{VIRTUAL_ROOT}{node_id}/",
                "connected": node is not None,
            }
            if node is not None:
                entry["agents"] = [
                    agent["provider"]
                    for agent in node.root.get("agents") or []
                    if isinstance(agent, Mapping) and isinstance(agent.get("provider"), str)
                ]
            listed.append(entry)
        return listed

    async def _subscribe(self, params: Mapping[str, Any], after: list[dict[str, Any]]) -> Any:
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise invalid_params("channel is required")
        if channel == ROOT_URI:
            return {"snapshot": self._subscribe_root()}
        snapshot = await self._subscribe_channel(channel, after)
        return {"snapshot": snapshot} if snapshot is not None else {}

    async def _subscribe_channel(
        self, channel: str, after: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        node_id = self._route({"channel": channel})
        # The buffer opens BEFORE any node is asked - including by the probe
        # below, whose subscription to the owner IS the real one. Opened any
        # later, whatever the owner streams between its subscribe and the
        # buffer existing would match neither the buffer nor a live
        # subscription, and be dropped.
        buffer: list[tuple[int, dict[str, Any]]] = []
        self.pending.setdefault(channel, []).append(buffer)
        try:
            if node_id is None:
                found = await self._probe_owner(channel)
                if found is None:
                    # The answer a host gives for a channel it does not know.
                    return None
                node_id, result = found
            else:
                result = await self._call(node_id, "subscribe", {"channel": channel})
        finally:
            buffers = self.pending[channel]
            buffers.remove(buffer)
            if not buffers:
                del self.pending[channel]
        node = self.nodes[node_id]
        node.subscribed.add(channel)
        self.subscriptions.add(channel)
        snapshot = result.get("snapshot") if isinstance(result, Mapping) else None
        if not isinstance(snapshot, Mapping):
            self._release(channel, buffer, 0, after)
            return None
        node_from = snapshot.get("fromSeq")
        from_seq = node.sequence.translate(node_from if isinstance(node_from, int) else 0)
        self._release(channel, buffer, from_seq, after)
        return {**snapshot, "fromSeq": from_seq}

    async def _probe_owner(self, channel: str) -> tuple[str, Any] | None:
        """Ask every node for `channel`; the first with a snapshot owns it.

        Reached when no node has named the channel yet - always the case on
        `reconnect`, where the surface's subscriptions come from a connection
        this one never saw. A node that does not know a channel answers
        `subscribe` with no snapshot, so the question is plain AHP.

        The owner's answer is kept as the subscription itself, not repeated:
        the caller's buffer has been open since before the first ask, so
        nothing the owner streams from here on can fall through. Only the
        nodes that turned out not to own it are unsubscribed.
        """
        owner: tuple[str, Any] | None = None
        for node_id in list(self.nodes):
            try:
                result = await self._call(node_id, "subscribe", {"channel": channel})
            except AhpError:
                continue
            found = isinstance(result, Mapping) and isinstance(result.get("snapshot"), Mapping)
            if found and owner is None:
                owner = (node_id, result)
                self.owners.claim(node_id, {channel})
                continue
            if found and owner is not None:
                # Two nodes claim one channel: two clients minted the same URI,
                # or a node answers for one it does not own. First writer wins,
                # as everywhere else, but it should not happen silently.
                _log.warning(
                    "channel %s is on %s and %s; keeping %s", channel, owner[0], node_id, owner[0]
                )
            node = self.nodes.get(node_id)
            if node is not None:
                node.link.notify("unsubscribe", {"channel": channel})
        return owner

    def _release(
        self,
        channel: str,
        buffer: list[tuple[int, dict[str, Any]]],
        from_seq: int,
        after: list[dict[str, Any]],
    ) -> None:
        # What the snapshot already contains must not be applied twice, and
        # nor must what an overlapping subscribe of the same channel released.
        floor = max(from_seq, self.delivered.get(channel, 0))
        for stamp, frame in buffer:
            if stamp > floor:
                after.append(frame)
                floor = stamp
        self.delivered[channel] = floor

    async def _list_sessions(self, params: Mapping[str, Any]) -> Any:
        limit = check_limit(params.get("limit"))
        cursor = params.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise invalid_params("cursor must be a string")
        positions = decode_cursor(cursor) if cursor is not None else {}

        async def fetch(node_id: str, node_cursor: str | None, count: int) -> Mapping[str, Any]:
            request: dict[str, Any] = {"channel": ROOT_URI, "limit": count}
            if node_cursor is not None:
                request["cursor"] = node_cursor
            result = await self._call(node_id, "listSessions", request)
            return result if isinstance(result, Mapping) else {}

        items, resumed = await merge_pages(list(self.nodes), positions, limit, fetch)
        result: dict[str, Any] = {"items": items}
        if has_more(resumed):
            result["nextCursor"] = encode_cursor(resumed)
        return result

    async def _create_session(self, params: Mapping[str, Any]) -> Any:
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise invalid_params("channel is required")
        node_id = self._node_for_new_session(params)
        # Claimed before the request, so an action the node publishes for the
        # new session ahead of its reply already has somewhere to go.
        self.owners.claim(node_id, {channel})
        return await self._call(node_id, "createSession", params)

    def _node_for_new_session(self, params: Mapping[str, Any]) -> str:
        named = self._node_named_by_files(params)
        if named is not None:
            return named
        provider = params.get("provider")
        if isinstance(provider, str):
            offering = self._nodes_offering(provider)
            if len(offering) == 1:
                return offering[0]
            if not offering:
                raise provider_not_found(provider)
            # Several nodes run it and nothing names one: a folder-less chat.
            default = self._default_node(provider)
            assert default is not None  # `offering` is non-empty and connected
            return default
        if len(self.nodes) == 1:
            return next(iter(self.nodes))
        raise invalid_params("createSession needs a provider or a working directory")

    async def _authenticate(self, params: Mapping[str, Any]) -> Any:
        """Hand a token to every node that could want it.

        `authenticate` rides the root channel and names only a resource, so
        nothing in it picks a node. It goes to each node whose agents
        advertise that resource; when none does (a challenge raised live, by
        an MCP server or a tool call), to every connected node, since the one
        that raised it will take the token and the rest refuse it. It succeeds
        if any node accepts, and fails with the first refusal if none does.
        """
        resource = params.get("resource")
        targets = [
            node_id
            for node_id, node in self.nodes.items()
            if any(
                isinstance(agent, Mapping)
                and any(
                    isinstance(meta, Mapping) and meta.get("resource") == resource
                    for meta in agent.get("protectedResources") or []
                )
                for agent in node.root.get("agents") or []
            )
        ] or list(self.nodes)
        if not targets:
            raise AhpError(-32603, "no node is connected")
        outcomes = await asyncio.gather(
            *(self._call(node_id, "authenticate", params) for node_id in targets),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if not isinstance(outcome, BaseException):
                return outcome
        first = outcomes[0]
        assert isinstance(first, BaseException)
        raise first

    def _nodes_offering(self, provider: str) -> list[str]:
        return [
            node_id
            for node_id, node in self.nodes.items()
            if any(
                isinstance(agent, Mapping) and agent.get("provider") == provider
                for agent in node.root.get("agents") or []
            )
        ]

    def _node_named_by_files(self, params: Mapping[str, Any]) -> str | None:
        """The node every file URI in the request names, if any names one.

        `workingDirectories` is where a session's directory travels
        (`CreateSessionParams`, and the `resolveSessionConfig` a client sends
        ahead of it); the single-URI keys cover the resource family.
        """
        candidates: list[Any] = [params.get(key) for key in _FILE_KEYS]
        directories = params.get("workingDirectories")
        if isinstance(directories, list):
            candidates.extend(directories)
        authorities = {
            authority for authority in (node_of(value) for value in candidates) if authority
        }
        if not authorities:
            return None
        if len(authorities) > 1:
            raise invalid_params(
                f"request names files on more than one node: {', '.join(sorted(authorities))}"
            )
        (authority,) = authorities
        if authority not in self.nodes:
            raise invalid_params(f"no node {authority!r} is available to this connection")
        return authority

    def _route(self, params: Mapping[str, Any]) -> str | None:
        for key in _CHANNEL_KEYS:
            value = params.get(key)
            if isinstance(value, str) and value != ROOT_URI:
                owner = self.owners.owner_of(value)
                if owner is None:
                    continue
                if owner not in self.nodes:
                    # Known, and gone. Falling through would hand the request
                    # to whichever node the later rules pick - the wrong one.
                    raise AhpError(-32603, f"node {owner!r} for {value} is not connected")
                return owner
        named = self._node_named_by_files(params)
        if named is not None:
            return named
        provider = params.get("provider")
        if isinstance(provider, str):
            offering = self._nodes_offering(provider)
            if len(offering) == 1:
                return offering[0]
        if len(self.nodes) == 1:
            return next(iter(self.nodes))
        return None

    async def _call(self, node_id: str, method: str, params: Mapping[str, Any]) -> Any:
        node = self.nodes.get(node_id)
        if node is None:
            raise AhpError(-32603, f"node {node_id!r} is not connected")
        try:
            outgoing = unqualify_file_uris(params, node_id, node.path_root)
        except ForeignUriError as exc:
            raise invalid_params(str(exc)) from exc
        result = qualify_file_uris(
            await node.link.request(method, outgoing), node_id, node.path_root
        )
        self.owners.claim(node_id, learn_owned_channels(result))
        return result

    # ─── surface notifications ───────────────────────────────────────────

    def _handle_notification(self, message: Mapping[str, Any]) -> None:
        if not self.initialized:
            return
        method = message.get("method")
        raw = message.get("params")
        params: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        if method == "unsubscribe":
            channel = params.get("channel")
            if not isinstance(channel, str):
                return
            self.subscriptions.discard(channel)
            for node in self.nodes.values():
                if channel in node.subscribed:
                    node.subscribed.discard(channel)
                    node.link.notify("unsubscribe", {"channel": channel})
        elif method == "dispatchAction":
            # A message's attachments and a chat's working directory arrive here.
            aliased = from_client_alias(params, set(self.records))
            params = aliased if isinstance(aliased, Mapping) else params
            channel = params.get("channel")
            if channel == ROOT_URI:
                # The merged root has no config to change (see core.root), and
                # no single node owns the others.
                return
            node_id = self._route(params)
            if node_id is None:
                _log.debug("dispatchAction for an unroutable channel: %r", channel)
                return
            target = self.nodes.get(node_id)
            if target is None:
                return
            try:
                outgoing = unqualify_file_uris(params, node_id, target.path_root)
            except ForeignUriError:
                _log.debug("dispatchAction naming another node's file dropped")
                return
            target.link.notify("dispatchAction", outgoing)
        # Unknown notifications are ignored, per the additive-change guarantee.

    # ─── node -> surface ─────────────────────────────────────────────────

    async def _pump(self, node: _Node) -> None:
        node_id = node.link.node_id
        try:
            async for frame in node.link.frames():
                try:
                    self._relay(node, frame)
                except Exception:
                    _log.exception("relaying a frame from %s failed", node_id)
        finally:
            if self.nodes.get(node_id) is node:
                self._drop_node(node_id)

    def _relay(self, node: _Node, frame: dict[str, Any]) -> None:
        node_id = node.link.node_id
        params = frame.get("params")
        params = params if isinstance(params, Mapping) else {}
        if frame.get("method") == "action":
            node_seq = params.get("serverSeq")
            # Stamped first and unconditionally, filtered or not: the
            # translation of a later `fromSeq` depends on every action this
            # link has carried, not only the ones the surface saw.
            stamp = node.sequence.stamp(node_seq if isinstance(node_seq, int) else None)
            envelope = qualify_file_uris(params, node_id, node.path_root)
            channel = envelope.get("channel")
            if channel == ROOT_URI:
                self._apply_root(node, envelope.get("action"))
                return
            self.owners.claim(node_id, learn_owned_channels(envelope))
            forwarded = {
                "jsonrpc": "2.0",
                "method": "action",
                "params": {**envelope, "serverSeq": stamp},
            }
            if isinstance(channel, str) and channel in self.pending:
                for buffer in self.pending[channel]:
                    buffer.append((stamp, forwarded))
            elif isinstance(channel, str) and channel in self.subscriptions:
                self.delivered[channel] = stamp
                self._send(forwarded)
            return
        qualified = qualify_file_uris(params, node_id, node.path_root)
        self.owners.claim(node_id, learn_owned_channels(qualified))
        if qualified.get("channel") == ROOT_URI and ROOT_URI not in self.subscriptions:
            return
        self._send({"jsonrpc": "2.0", "method": frame.get("method"), "params": qualified})

    def _apply_root(self, node: _Node, action: Any) -> None:
        if not isinstance(action, Mapping):
            return
        reduced = REDUCERS["root"](node.root, action)
        node.root = reduced if isinstance(reduced, dict) else node.root
        self.owners.claim(node.link.node_id, learn_owned_channels(node.root))
        self._republish_root()

    def _republish_root(self) -> None:
        before, self.root = self.root, merge_root([node.root for node in self.nodes.values()])
        if ROOT_URI not in self.subscriptions:
            return
        for action in root_actions(before, self.root):
            self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "action",
                    "params": {
                        "channel": ROOT_URI,
                        "action": action,
                        "serverSeq": self.clock.tick(),
                    },
                }
            )

    def _drop_node(self, node_id: str) -> None:
        _log.warning("node %s link closed", node_id)
        self.nodes.pop(node_id, None)
        # Ownership is kept on purpose: a request for one of this node's
        # channels must be refused as "not connected", not rerouted to
        # whichever node the fallback rules would pick.
        self._republish_root()
        if not self.closing and node_id in self.records:
            self._spawn(self._redial(node_id))

    # ─── node -> surface requests ────────────────────────────────────────

    def _path_root(self, node_id: str) -> str | None:
        node = self.nodes.get(node_id)
        return node.path_root if node is not None else None

    async def _node_request(self, node_id: str, method: str, params: Mapping[str, Any]) -> Any:
        request_id = self.next_outbound_id
        self.next_outbound_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.outbound[request_id] = future
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": qualify_file_uris(params, node_id, self._path_root(node_id)),
            }
        )
        try:
            result = await future
        finally:
            self.outbound.pop(request_id, None)
        try:
            return unqualify_file_uris(result, node_id, self._path_root(node_id))
        except ForeignUriError as exc:
            raise invalid_params(str(exc)) from exc

    def _resolve_outbound(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        future = self.outbound.get(request_id) if isinstance(request_id, int) else None
        if future is None or future.done():
            _log.debug("dropping unmatched response frame")
            return
        error = message.get("error")
        if isinstance(error, Mapping):
            code = error.get("code")
            future.set_exception(
                AhpError(
                    code if isinstance(code, int) else -32603,
                    str(error.get("message", "")),
                    error.get("data"),
                )
            )
        else:
            future.set_result(message.get("result"))
