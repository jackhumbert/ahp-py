"""The host: JSON-RPC dispatch, the command set, and the agent event mapper.

Implements the v0.1 command surface -- `initialize`, `ping`, `subscribe`,
`unsubscribe`, `listSessions`, `createSession`, `dispatchAction`, `reconnect` --
and answers `MethodNotFound` for everything else. There are no silent stubs:
because the protocol has no server capability object, that error *is* how a host
declines a feature.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_host_server.core import errors
from agent_host_server.core.channels import ROOT_URI, ChannelKind, classify
from agent_host_server.core.connection import Connection
from agent_host_server.core.policy import Policy
from agent_host_server.core.sequencer import Sequencer
from agent_host_server.core.turn import TurnRunner
from agent_host_server.core.versions import DEFAULT_SUPPORTED_VERSIONS, negotiate
from agent_host_server.core.wirelog import WireLog
from agent_host_server.provider.base import (
    AgentProvider,
    AgentSession,
    AgentSessionContext,
    DescribesSession,
)
from agent_host_server.reducers.clock import now_iso
from agent_host_server.transport.base import Transport
from agent_host_server.types import IS_CLIENT_DISPATCHABLE
from agent_host_server.types.protocol import SessionStatus

__all__ = ["Host", "HostInfo"]

_log = logging.getLogger(__name__)

#: SessionStatus.Idle -- what a freshly created session reports.
_STATUS_IDLE = SessionStatus.IDLE


@dataclass(frozen=True)
class HostInfo:
    """`InitializeResult.serverInfo`. Informational only; never feature-detect on it."""

    name: str = "agent-host-server"
    version: str = "0.0.0"

    def to_wire(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version}


@dataclass
class _Session:
    uri: str
    chat_uri: str
    provider_id: str
    title: str
    created_at: str
    agent_session: AgentSession | None = None
    turn: asyncio.Task[None] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Host:
    """An AHP host over one or more client connections.

    A :class:`~agent_host_server.core.policy.Policy` is **required**: there is no
    default and no convenience function that binds a socket. Every trust
    decision belongs to the embedding application (ADR/`policy.py`).
    """

    def __init__(
        self,
        provider: AgentProvider,
        policy: Policy,
        *,
        info: HostInfo | None = None,
        supported_versions: Sequence[str] = DEFAULT_SUPPORTED_VERSIONS,
        wire_log: Path | None = None,
    ) -> None:
        if policy is None:  # pragma: no cover - defensive; typing already forbids it
            raise ValueError("a Policy is required; there is no default")
        self.provider = provider
        self.policy = policy
        self.info = info or HostInfo()
        self.supported_versions = tuple(supported_versions)
        self.sequencer = Sequencer()
        self._sessions: dict[str, _Session] = {}
        self._connections: set[Connection] = set()
        self._background: set[asyncio.Task[None]] = set()
        self._root_ready = False
        self.wire_log = WireLog(wire_log) if wire_log is not None else None

    # ─── lifecycle ───────────────────────────────────────────────────────

    async def _ensure_root(self) -> None:
        if not self._root_ready:
            await self.sequencer.register_channel(
                ROOT_URI,
                {"agents": [self.provider.agent.to_wire()], "activeSessions": 0},
                "root",
            )
            self._root_ready = True

    async def serve(self, transport: Transport, *, peer: str | None = None) -> None:
        """Drive one client connection until its transport closes."""
        await self._ensure_root()
        connection = Connection(transport, peer=peer, wire_log=self.wire_log)
        connection.start_writer()
        self._connections.add(connection)
        pending: set[asyncio.Task[None]] = set()
        try:
            while True:
                message = await transport.receive()
                if message is None:
                    return
                if self.wire_log is not None:
                    self.wire_log.record("c2s", message, connection.client_id or "?")
                # Requests run as tasks so a slow one cannot block the next
                # message on this connection. Notifications are handled inline,
                # preserving per-channel arrival order for dispatchAction.
                if "id" in message and "method" in message:
                    task = asyncio.create_task(self._handle_request(connection, message))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                elif "method" in message:
                    # A notification has no response, so a fault here has
                    # nowhere to go -- and letting it escape would end the read
                    # loop and drop a connection over one bad frame from an
                    # untrusted peer.
                    try:
                        await self._handle_notification(connection, message)
                    except Exception:
                        _log.exception("notification handler failed: %s", message.get("method"))
        finally:
            for task in list(pending):
                task.cancel()
            await self.sequencer.unsubscribe_all(connection)
            self._connections.discard(connection)
            await connection.close()

    # ─── dispatch ────────────────────────────────────────────────────────

    async def _handle_request(self, connection: Connection, message: Mapping[str, Any]) -> None:
        request_id = message["id"]
        method = message["method"]
        params = message.get("params") or {}
        try:
            result = await self._dispatch(connection, method, params)
        except errors.AhpError as exc:
            connection.enqueue({"jsonrpc": "2.0", "id": request_id, "error": exc.to_json()})
            return
        except Exception as exc:
            connection.enqueue(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": errors.internal_error(f"{type(exc).__name__}: {exc}").to_json(),
                }
            )
            return
        connection.enqueue({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _dispatch(
        self, connection: Connection, method: str, params: Mapping[str, Any]
    ) -> Any:
        if method == "initialize":
            return await self._initialize(connection, params)
        if method == "ping":
            # "the server MUST respond regardless of whether the client has
            # completed `initialize` or holds any subscriptions"
            # (docs/specification/transport.md, Keep-Alive). A ping is how a
            # client keeps an idle-timeout intermediary from closing the socket,
            # so it cannot be gated on anything.
            return None
        if method == "reconnect":
            # A valid FIRST request: it re-establishes a connection that dropped,
            # so there is no prior `initialize` on *this* transport. VS Code
            # opens with `reconnect` whenever it has a remembered serverSeq and
            # subscription set, and does not fall back to `initialize` if we
            # refuse -- it just retries, forever.
            return await self._reconnect(connection, params)
        if not connection.initialized:
            raise errors.invalid_params("initialize must be the first request")
        if method == "subscribe":
            return await self._subscribe(connection, params)
        if method == "listSessions":
            return await self._list_sessions(connection, params)
        if method == "createSession":
            return await self._create_session(connection, params)
        if method == "disposeSession":
            return await self._dispose_session(connection, params)
        raise errors.method_not_found(method)

    async def _handle_notification(
        self, connection: Connection, message: Mapping[str, Any]
    ) -> None:
        method = message["method"]
        params = message.get("params") or {}
        if method == "unsubscribe":
            channel = params.get("channel")
            if isinstance(channel, str):
                await self.sequencer.unsubscribe(connection, channel)
        elif method == "dispatchAction":
            await self._dispatch_action(connection, params)
        # Unknown notifications are ignored, per the additive-change guarantee.

    # ─── commands ────────────────────────────────────────────────────────

    async def _initialize(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        offered = params.get("protocolVersions")
        if not isinstance(offered, list) or not offered:
            raise errors.invalid_params("protocolVersions must be a non-empty array")

        chosen = negotiate(offered, self.supported_versions)
        if chosen is None:
            # MUST refuse rather than proceed. No client verifies this for us.
            raise errors.unsupported_protocol_version(self.supported_versions)

        client_id = params.get("clientId")
        connection.client_id = client_id if isinstance(client_id, str) else str(uuid.uuid4())
        connection.protocol_version = chosen

        if not self.policy.authorize_connection(connection.info):
            raise errors.AhpError(-32009, "Connection refused by policy")

        connection.initialized = True

        snapshots: list[dict[str, Any]] = []
        for uri in params.get("initialSubscriptions") or []:
            if not isinstance(uri, str) or not self.policy.may_see_channel(connection.info, uri):
                continue
            snapshot = await self.sequencer.subscribe(connection, uri)
            if snapshot is not None:
                snapshots.append(snapshot)

        # `snapshots` is ALWAYS an array. MultiHostClient calls .find() on it
        # with no guard, so omitting it puts the client in an endless reconnect
        # loop rather than surfacing an error.
        return {
            "protocolVersion": chosen,
            "serverSeq": self.sequencer.server_seq,
            "serverInfo": self.info.to_wire(),
            "snapshots": snapshots,
        }

    async def _subscribe(self, connection: Connection, params: Mapping[str, Any]) -> dict[str, Any]:
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise errors.invalid_params("channel is required")
        if not self.policy.may_see_channel(connection.info, channel):
            raise errors.AhpError(-32009, f"Not permitted to observe {channel}")
        snapshot = await self.sequencer.subscribe(connection, channel)
        # A stateless or unknown channel yields `{}` -- the shape the spec gives
        # for channels that carry no snapshot.
        return {"snapshot": snapshot} if snapshot is not None else {}

    async def _list_sessions(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        del params  # `limit`/`cursor` accepted and ignored: we never paginate yet.
        items = [
            {
                "resource": session.uri,
                "provider": session.provider_id,
                "title": session.title,
                "status": _STATUS_IDLE,
                "createdAt": session.created_at,
                "modifiedAt": session.created_at,
            }
            for session in self._sessions.values()
            if self.policy.may_see_channel(connection.info, session.uri)
        ]
        # A *successful* listSessions MUST carry `items`: the client's
        # `for...of summaries.items` sits outside its try/catch, so a malformed
        # success is fatal where an error response would have been tolerated.
        return {"items": items}

    async def _create_session(self, connection: Connection, params: Mapping[str, Any]) -> None:
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise errors.invalid_params("channel is required")
        # The session URI is CLIENT-CHOSEN and opaque. Never parse or validate
        # its shape: real clients use forms other than ahp-session:/<uuid>.
        if channel in self._sessions:
            raise errors.already_exists(channel)
        if not self.policy.may_create_session(connection.info, params):
            raise errors.AhpError(-32009, "Not permitted to create a session")

        provider_id = params.get("provider") or self.provider.agent.provider
        chat_uri = f"ahp-chat:/{uuid.uuid4()}"
        created_at = now_iso()
        session = _Session(
            uri=channel,
            chat_uri=chat_uri,
            provider_id=provider_id,
            title="New Session",
            created_at=created_at,
        )
        self._sessions[channel] = session

        await self.sequencer.register_channel(
            channel,
            {
                "provider": provider_id,
                "title": session.title,
                "status": _STATUS_IDLE,
                "lifecycle": "creating",
                "activeClients": [],
                "chats": [],
            },
            "session",
        )
        await self.sequencer.register_channel(
            chat_uri,
            {
                "resource": chat_uri,
                "title": session.title,
                "status": _STATUS_IDLE,
                "modifiedAt": created_at,
                "turns": [],
            },
            "chat",
        )

        # Bring-up runs after the response so the client can subscribe first.
        # Hold a reference: a bare create_task can be garbage-collected mid-flight.
        task = asyncio.create_task(self._bring_up(session, params))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return

    async def _bring_up(self, session: _Session, params: Mapping[str, Any]) -> None:
        try:
            context = AgentSessionContext(
                session_uri=session.uri,
                chat_uri=session.chat_uri,
                provider_id=session.provider_id,
                working_directories=tuple(params.get("workingDirectories") or ()),
                config=params.get("config") or {},
            )
            session.agent_session = await self.provider.create_session(context)
        except Exception as exc:
            await self.sequencer.publish(
                session.uri,
                {
                    "type": "session/creationFailed",
                    "error": {"message": f"{type(exc).__name__}: {exc}"},
                },
            )
            return

        summary = {
            "resource": session.uri,
            "provider": session.provider_id,
            "title": session.title,
            "status": _STATUS_IDLE,
            "createdAt": session.created_at,
            "modifiedAt": session.created_at,
        }
        await self.sequencer.notify(
            ROOT_URI, "root/sessionAdded", {"channel": ROOT_URI, "summary": summary}
        )
        await self.sequencer.publish(
            ROOT_URI,
            {"type": "root/activeSessionsChanged", "activeSessions": len(self._sessions)},
        )
        chat_summary = {
            "resource": session.chat_uri,
            "title": session.title,
            "status": _STATUS_IDLE,
            "modifiedAt": session.created_at,
        }
        await self.sequencer.publish(
            session.uri, {"type": "session/chatAdded", "summary": chat_summary}
        )
        await self.sequencer.publish(
            session.uri,
            {"type": "session/defaultChatChanged", "defaultChat": session.chat_uri},
        )
        # Customizations and tools are session STATE, so they are published as
        # actions before `ready` -- a client subscribing on ready then sees them
        # in its snapshot rather than racing for them.
        if isinstance(session.agent_session, DescribesSession):
            described = await session.agent_session.describe()
            if described.customizations:
                await self.sequencer.publish(
                    session.uri,
                    {
                        "type": "session/customizationsChanged",
                        "customizations": list(described.customizations),
                    },
                )
            if described.server_tools:
                await self.sequencer.publish(
                    session.uri,
                    {"type": "session/serverToolsChanged", "tools": list(described.server_tools)},
                )

        await self.sequencer.publish(session.uri, {"type": "session/ready"})

    async def _dispose_session(self, connection: Connection, params: Mapping[str, Any]) -> None:
        """Tear the session down, drop its channels, and tell the root channel.

        The spec: "the server tears down the session backend, drops associated
        subscriptions, and broadcasts `root/sessionRemoved`".
        """
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise errors.invalid_params("channel is required")
        session = self._sessions.get(channel)
        if session is None:
            raise errors.session_not_found(channel)
        if not self.policy.may_see_channel(connection.info, channel):
            raise errors.AhpError(-32009, f"Not permitted to dispose {channel}")

        if session.turn is not None:
            session.turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.turn
        if session.agent_session is not None:
            await session.agent_session.aclose()

        del self._sessions[channel]
        await self.sequencer.drop_channel(session.chat_uri)
        await self.sequencer.drop_channel(channel)

        await self.sequencer.notify(
            ROOT_URI, "root/sessionRemoved", {"channel": ROOT_URI, "session": channel}
        )
        await self.sequencer.publish(
            ROOT_URI,
            {"type": "root/activeSessionsChanged", "activeSessions": len(self._sessions)},
        )
        return

    async def _reconnect(self, connection: Connection, params: Mapping[str, Any]) -> dict[str, Any]:
        # `reconnect` establishes the connection when it arrives first. There is
        # no version to negotiate -- it carries no `protocolVersions` -- so we
        # adopt our most-preferred one. It also carries no credential: it
        # resumes on a client-asserted `clientId` alone, which is exactly why
        # admission is the Policy's decision and not this method's.
        if not connection.initialized:
            client_id = params.get("clientId")
            connection.client_id = client_id if isinstance(client_id, str) else str(uuid.uuid4())
            connection.protocol_version = self.supported_versions[0]
            if not self.policy.authorize_connection(connection.info):
                raise errors.AhpError(-32009, "Connection refused by policy")
            connection.initialized = True

        subscriptions = [
            uri
            for uri in params.get("subscriptions") or []
            if isinstance(uri, str) and self.policy.may_see_channel(connection.info, uri)
        ]
        for uri in subscriptions:
            await self.sequencer.subscribe(connection, uri)
        last_seen = params.get("lastSeenServerSeq")
        return await self.sequencer.replay(
            last_seen if isinstance(last_seen, int) else 0, subscriptions
        )

    # ─── client-dispatched actions ───────────────────────────────────────

    async def _dispatch_action(self, connection: Connection, params: Mapping[str, Any]) -> None:
        channel = params.get("channel")
        action = params.get("action")
        if not isinstance(channel, str) or not isinstance(action, Mapping):
            return
        if not self.sequencer.has_channel(channel):
            # Unknown channel: silently ignore, no echo. Specified asymmetry.
            return

        # `dispatchAction` carries only {channel, clientSeq, action} -- no
        # clientId -- so the host stamps origin from the connection. Getting
        # this wrong silently breaks optimistic reconciliation for every client
        # except the originator.
        client_seq = params.get("clientSeq")
        origin = {
            "clientId": connection.client_id,
            "clientSeq": client_seq if isinstance(client_seq, int) else 0,
        }

        rejection = self._validate_client_action(connection, channel, action)
        if rejection is not None:
            await self.sequencer.publish(channel, action, origin=origin, rejection_reason=rejection)
            return

        await self.sequencer.publish(channel, action, origin=origin)
        await self._react(channel, action)

    def _validate_client_action(
        self, connection: Connection, channel: str, action: Mapping[str, Any]
    ) -> str | None:
        """Return a rejection reason, or ``None`` to accept.

        Invalid client actions MUST be echoed back with `rejectionReason` so the
        client can revert its optimistic prediction -- dropping them silently
        leaves that prediction applied forever.
        """
        action_type = action.get("type")
        if not isinstance(action_type, str):
            return "action has no type"
        # Protocol invariant, checked unconditionally and before any policy:
        # a client may only originate actions marked client-dispatchable.
        if not IS_CLIENT_DISPATCHABLE.get(action_type, False):
            return f"{action_type} is not client-dispatchable"
        if not self.policy.may_dispatch(connection.info, channel, action):
            return "rejected by policy"

        state = self.sequencer.state_of(channel)
        if classify(channel) is ChannelKind.CHAT and isinstance(state, Mapping):
            if action_type == "chat/turnCancelled" and state.get("activeTurn") is None:
                return "no active turn to cancel"
            if action_type == "chat/turnStarted" and state.get("activeTurn") is not None:
                return "a turn is already active"
        return None

    async def _react(self, channel: str, action: Mapping[str, Any]) -> None:
        """Side effects a client action triggers on the agent."""
        action_type = action.get("type")
        session = next((s for s in self._sessions.values() if s.chat_uri == channel), None)
        if session is None:
            return
        if action_type == "chat/turnStarted":
            runner = TurnRunner(self.sequencer, channel)
            session.turn = asyncio.create_task(runner.run(session.agent_session, action))
        elif action_type == "chat/turnCancelled" and session.turn is not None:
            session.turn.cancel()
            if session.agent_session is not None:
                await session.agent_session.cancel("client cancelled")

    # ─── shutdown ────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        for session in self._sessions.values():
            if session.turn is not None:
                session.turn.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await session.turn
            if session.agent_session is not None:
                await session.agent_session.aclose()
        for connection in list(self._connections):
            await connection.close()
