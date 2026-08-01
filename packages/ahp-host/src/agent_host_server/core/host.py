"""The host: JSON-RPC dispatch, the command set, and the agent event mapper.

Implements the v0.1 command surface -- `initialize`, `ping`, `subscribe`,
`unsubscribe`, `listSessions`, `createSession`, `dispatchAction`, `reconnect` --
and answers `MethodNotFound` for everything else. There are no silent stubs:
because the protocol has no server capability object, that error *is* how a host
declines a feature.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from agent_host_server.core import errors
from agent_host_server.core.channels import ROOT_URI
from agent_host_server.core.config import RootConfig, type_matches
from agent_host_server.core.connection import Connection
from agent_host_server.core.pending import PendingRequests, RequestOutcome
from agent_host_server.core.policy import Policy
from agent_host_server.core.seq import FileSequence
from agent_host_server.core.sequencer import Sequencer
from agent_host_server.core.turn import TurnRunner
from agent_host_server.core.versions import DEFAULT_SUPPORTED_VERSIONS, negotiate
from agent_host_server.core.wirelog import WireLog
from agent_host_server.provider.base import (
    AgentProvider,
    AgentSession,
    AgentSessionContext,
    ConfigRequest,
    ConfiguresSessions,
    DescribesSession,
    HandlesCustomizations,
    SessionPublisher,
)
from agent_host_server.reducers.clock import now_iso
from agent_host_server.transport.base import Transport
from agent_host_server.types import IS_CLIENT_DISPATCHABLE
from agent_host_server.types.protocol import SessionStatus, session_status_flags

__all__ = ["Host", "HostInfo"]

_log = logging.getLogger(__name__)

#: SessionStatus.Idle -- what a freshly created session reports.
_STATUS_IDLE = SessionStatus.IDLE

#: The mutable half of `SessionSummary`. `SessionState` "inlines (denormalizes)
#: every SessionMetadata field directly onto itself", so the catalogue summary is
#: a straight projection of the session channel's own state -- which is how the
#: host keeps the two in sync without enumerating action types. `resource`,
#: `provider` and `createdAt` are identity and never appear in a change set.
_SUMMARY_FIELDS: Final = (
    "title",
    "status",
    "activity",
    "project",
    "workingDirectories",
    "annotations",
    "changes",
    "_meta",
)

#: Upper bound on one `listSessions` page. The spec lets a server "impose its
#: own upper cap"; without one, a host with a large catalogue serialises the
#: whole thing into a single response.
_MAX_PAGE = 200

#: Client-dispatchable, and between them they name the filesystem roots the
#: agent gets tool access to.
_WORKING_DIRECTORY_ACTIONS: Final = frozenset(
    {"session/workingDirectorySet", "session/workingDirectoryRemoved"}
)

#: Client-dispatchable, and both name a request the host is suspended on.
#: Upstream states their rejection rules in prose ("servers SHOULD reject...")
#: and the reducers enforce none of them.
_INPUT_ACTIONS: Final = frozenset({"chat/inputAnswerChanged", "chat/inputCompleted"})

#: Client-dispatchable, and each resolves a tool call the host is suspended on.
#: The protocol's own validation table conditions `chat/toolCallConfirmed` on
#: the call's STATUS and never on client identity -- so any subscriber may
#: approve any other client's pending call. That is upstream's design; the host
#: still has to check the call is actually pending, which no reducer does.
_TOOL_RESOLVING_ACTIONS: Final = frozenset({"chat/toolCallConfirmed", "chat/toolCallComplete"})


def _active_clients(active_client: Any) -> list[Any]:
    """`createSession.activeClient` as the initial `activeClients` list.

    `tools` is required on `SessionActiveClient` and a client that omits it is
    malformed, but the list is fanned out to every subscriber and a missing key
    would break their iteration -- so it is normalised, not rejected. Everything
    else on the entry survives verbatim (ADR 0001).
    """
    if not isinstance(active_client, Mapping):
        return []
    if not isinstance(active_client.get("clientId"), str):
        # Without a clientId the entry is unaddressable: nothing can update it,
        # remove it, or be told to execute its tools.
        return []
    entry = dict(active_client)
    if not isinstance(entry.get("tools"), list):
        entry["tools"] = []
    return [entry]


def _answers_of(state: Any, request_id: str) -> Mapping[str, Any]:
    """The final answers on an input-request part, read from the reduced state.

    The part is "both the live interaction and its durable record": drafts land
    on it while the request is open and `chat/inputCompleted` overlays its own
    answers onto them. Reading it after the reducer runs is the only way to see
    the merge without re-implementing it.
    """
    if not isinstance(state, Mapping):
        return {}
    active = state.get("activeTurn")
    parts = active.get("responseParts") if isinstance(active, Mapping) else None
    for part in parts if isinstance(parts, list) else ():
        if not isinstance(part, Mapping) or part.get("kind") != "inputRequest":
            continue
        request = part.get("request")
        if not isinstance(request, Mapping) or request.get("id") != request_id:
            continue
        answers = request.get("answers")
        return answers if isinstance(answers, Mapping) else {}
    return {}


def _encode_cursor(summary: Mapping[str, Any]) -> str:
    """A keyset cursor: the sort key of the last entry on the page.

    Keyset rather than an offset because the catalogue mutates between pages --
    an offset silently skips an entry when a session is disposed mid-walk. The
    encoding is opaque by contract ("clients MUST NOT parse, modify, or persist
    them"); base64url is chosen only so it survives a client that logs it.
    """
    payload = json.dumps([summary["modifiedAt"], summary["resource"]], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        modified_at, resource = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return str(modified_at), str(resource)
    except (ValueError, TypeError):
        # "An unrecognised cursor SHOULD be rejected with an `InvalidParams`
        # error" -- rather than silently restarting from the first page, which
        # would make a client's paging loop never terminate.
        raise errors.invalid_params("unrecognised pagination cursor") from None


class _Publisher:
    """The host's `SessionPublisher`: out-of-turn state, mapped to actions.

    Held by the provider for the life of the session, so every method has to
    tolerate the session having been disposed underneath it. `Sequencer.publish`
    already answers `None` for a channel that no longer exists, so this is
    naturally safe -- but `root/progress` goes through `notify`, which does not
    reduce anything, so it is guarded by the token instead.
    """

    def __init__(self, host: Host, session: _Session, progress_token: str | None) -> None:
        self._host = host
        self._session = session
        self._progress_token = progress_token

    async def customizations_changed(
        self,
        customizations: Sequence[Mapping[str, Any]],
        server_tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        await self._host.sequencer.publish(
            self._session.uri,
            {"type": "session/customizationsChanged", "customizations": list(customizations)},
        )
        if server_tools is not None:
            await self._host.sequencer.publish(
                self._session.uri,
                {"type": "session/serverToolsChanged", "tools": list(server_tools)},
            )

    async def activity_changed(self, activity: str | None) -> None:
        action: dict[str, Any] = {"type": "session/activityChanged"}
        if activity is not None:
            action["activity"] = activity
        await self._host.sequencer.publish(self._session.uri, action)
        await self._host._mirror_summary(self._session)

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        # "Echoes the `progressToken` the client supplied on the originating
        # request" -- with no token there is nothing to correlate to, so there is
        # nothing to send. A provider may therefore call this unconditionally.
        if self._progress_token is None:
            return
        params: dict[str, Any] = {
            "channel": ROOT_URI,
            "progressToken": self._progress_token,
            "progress": progress,
        }
        if total is not None:
            params["total"] = total
        if message is not None:
            params["message"] = message
        await self._host.sequencer.notify(ROOT_URI, "root/progress", params)


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
    #: The summary the root channel was last told about. `root/sessionSummaryChanged`
    #: carries only fields that changed, so the host has to remember what it sent.
    published_summary: dict[str, Any] = field(default_factory=dict)
    #: Handed to the provider, and kept here so the host can publish on the
    #: session's behalf too.
    publisher: SessionPublisher | None = None

    @property
    def annotations_uri(self) -> str:
        """ "The channel URI is derived from the session URI by appending
        `/annotations`." One per session, always."""
        return f"{self.uri}/annotations"


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
        sequence_file: Path | None = None,
        root_config: RootConfig | None = None,
    ) -> None:
        if policy is None:  # pragma: no cover - defensive; typing already forbids it
            raise ValueError("a Policy is required; there is no default")
        self.provider = provider
        self.policy = policy
        self.info = info or HostInfo()
        self.supported_versions = tuple(supported_versions)
        # Without `sequence_file` the counter restarts with the process, which
        # is right for a host whose sessions do not outlive it and wrong for one
        # whose do -- see `core/seq.py`. No path is chosen on the embedder's
        # behalf.
        self.sequencer = Sequencer(
            allocator=FileSequence(sequence_file) if sequence_file is not None else None
        )
        #: Every provider request suspended on a client. ADR 0005 -- one
        #: registry, so elicitation, tool confirmation and auth step-up cannot
        #: each grow their own lifetime and cancellation rules.
        self.pending = PendingRequests()
        self._sessions: dict[str, _Session] = {}
        self._connections: set[Connection] = set()
        self._background: set[asyncio.Task[None]] = set()
        # No default schema, for the same reason there is no default Policy.
        # Without one `RootState.config` stays absent, the reducer's own guard
        # drops every `root/configChanged`, and the host accepts nothing -- the
        # status quo, but now on purpose rather than by accident.
        self.root_config = root_config
        self._root_ready = False
        self.wire_log = WireLog(wire_log) if wire_log is not None else None

    # ─── lifecycle ───────────────────────────────────────────────────────

    async def _ensure_root(self) -> None:
        if not self._root_ready:
            root_state: dict[str, Any] = {
                "agents": [self.provider.agent.to_wire()],
                "activeSessions": 0,
            }
            if self.root_config is not None:
                root_state["config"] = self.root_config.to_wire()
            await self.sequencer.register_channel(ROOT_URI, root_state, "root")
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
            await self._retire_active_client(connection)
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
        if method == "fetchTurns":
            return await self._fetch_turns(connection, params)
        if method == "resolveSessionConfig":
            return await self._resolve_session_config(params)
        if method == "sessionConfigCompletions":
            return await self._session_config_completions(params)
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

    # ─── session summaries ───────────────────────────────────────────────
    #
    # The root channel's catalogue and the session channel's state are two views
    # of the same thing -- `SessionState` inlines every `SessionMetadata` field
    # -- and the spec makes keeping them in sync the host's job: "the host keeps
    # the two in sync via `root/sessionSummaryChanged`".
    #
    # So rather than enumerate the actions that mutate a summary field, the host
    # projects the session channel's state after every publish and emits the
    # difference. That is automatically right for actions we do not emit yet, and
    # it cannot drift when a new one is added.

    def _project_summary(self, session: _Session) -> dict[str, Any]:
        """The mutable half of the session's `SessionSummary`, derived from state."""
        state = self.sequencer.state_of(session.uri)
        summary: dict[str, Any] = {}
        if isinstance(state, Mapping):
            summary = {key: state[key] for key in _SUMMARY_FIELDS if key in state}

        # Aggregation across chats, per `SessionSummary`'s producer rules: take
        # the activity bits from the default chat and the max of every chat's
        # `modifiedAt`. With one chat both reduce to that chat's values -- but
        # writing it as an aggregate now means multi-chat does not have to
        # rediscover the rule. Session-scoped flag bits (IsRead, IsArchived)
        # stay with the session and are not overwritten.
        chat = self.sequencer.state_of(session.chat_uri)
        if isinstance(chat, Mapping):
            chat_status = chat.get("status")
            if isinstance(chat_status, int):
                session_flags = summary.get("status", _STATUS_IDLE)
                flags = session_flags if isinstance(session_flags, int) else _STATUS_IDLE
                summary["status"] = session_status_flags(
                    (flags & ~SessionStatus.ACTIVITY_MASK)
                    | (chat_status & SessionStatus.ACTIVITY_MASK)
                )
            modified = chat.get("modifiedAt")
            if isinstance(modified, str):
                summary["modifiedAt"] = max(modified, session.created_at)
        summary.setdefault("modifiedAt", session.created_at)

        # `SessionSummary.annotations` lets badge UI render counts "without
        # subscribing to the channel itself", so it is derived here rather than
        # left to a producer to remember. Absent would mean "this session
        # exposes no annotations channel", which is no longer true.
        annotations = self.sequencer.state_of(session.annotations_uri)
        if isinstance(annotations, Mapping):
            entries = annotations.get("annotations")
            entries = entries if isinstance(entries, list) else []
            summary["annotations"] = {
                "resource": session.annotations_uri,
                "annotationCount": len(entries),
                "entryCount": sum(
                    len(e["entries"])
                    for e in entries
                    if isinstance(e, Mapping) and isinstance(e.get("entries"), list)
                ),
            }
        return summary

    def _full_summary(self, session: _Session) -> dict[str, Any]:
        """Identity fields plus the projection. What `listSessions` returns."""
        return {
            "resource": session.uri,
            "provider": session.provider_id,
            "createdAt": session.created_at,
            **self._project_summary(session),
        }

    async def _mirror_summary(self, session: _Session) -> None:
        """Emit `root/sessionSummaryChanged` for whatever actually changed.

        "Only fields present in `changes` have new values; omitted fields are
        unchanged on the client's cached summary." Sending nothing when nothing
        changed matters: the client caches a session list and a no-op
        notification per streamed delta would be a notification per token.
        """
        current = self._project_summary(session)
        changes = {
            key: value
            for key, value in current.items()
            if key not in session.published_summary or session.published_summary[key] != value
        }
        if not changes:
            return
        session.published_summary = current
        session.title = current.get("title", session.title)
        await self.sequencer.notify(
            ROOT_URI,
            "root/sessionSummaryChanged",
            {"channel": ROOT_URI, "session": session.uri, "changes": changes},
        )

    def _session_for(self, channel: str) -> _Session | None:
        """The session owning a session, chat or annotations channel.

        A reverse lookup over the sessions the host minted, not a parse of the
        URI (invariant 15) -- `<session>/annotations` happens to be derivable,
        but session and chat URIs are client-chosen and opaque.
        """
        session = self._sessions.get(channel)
        if session is not None:
            return session
        return next(
            (s for s in self._sessions.values() if channel in (s.chat_uri, s.annotations_uri)),
            None,
        )

    async def _list_sessions(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        # "The server SHOULD return most-recently-modified entries first, so the
        # first page is the immediately useful one." The URI breaks ties into a
        # total order, without which a cursor could skip or repeat an entry.
        visible = sorted(
            (
                self._full_summary(session)
                for session in self._sessions.values()
                if self.policy.may_see_channel(connection.info, session.uri)
            ),
            key=lambda summary: (summary["modifiedAt"], summary["resource"]),
            reverse=True,
        )

        cursor = params.get("cursor")
        if cursor is not None:
            if not isinstance(cursor, str):
                raise errors.invalid_params("cursor must be a string")
            after = _decode_cursor(cursor)
            visible = [s for s in visible if (s["modifiedAt"], s["resource"]) < after]

        limit = params.get("limit")
        size = limit if isinstance(limit, int) and 0 < limit <= _MAX_PAGE else _MAX_PAGE
        page, rest = visible[:size], visible[size:]

        # A *successful* listSessions MUST carry `items`: the client's
        # `for...of summaries.items` sits outside its try/catch, so a malformed
        # success is fatal where an error response would have been tolerated.
        result: dict[str, Any] = {"items": page}
        if rest:
            # "A missing `nextCursor` signals the end of the collection" -- so it
            # is set only when there is genuinely another page, never eagerly.
            result["nextCursor"] = _encode_cursor(page[-1])
        return result

    async def _fetch_turns(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Load older turns into a chat. There are never any: state is in memory.

        This host keeps every turn of a chat in that chat's state and therefore
        never sets `ChatState.turnsNextCursor`, so there is no older page to
        page in. It is still implemented rather than refused, because the
        alternative reading -- `MethodNotFound` -- is wrong twice over: the
        command *is* supported, and a client cannot distinguish "this host does
        not page" from "this host is broken".

        Two MUSTs are honoured literally:

        * "the host MUST dispatch `chat/turnsLoaded` ... before responding" is
          unconditional, so an empty load still dispatches. With no turns and no
          cursor the reducer's result is the identity, which is the honest
          answer: nothing older exists.
        * "The host MUST reject unrecognised cursors with `InvalidParams`." We
          have never issued one, so every cursor is unrecognised.
        """
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise errors.invalid_params("channel is required")
        if not self.policy.may_see_channel(connection.info, channel):
            raise errors.AhpError(-32009, f"Not permitted to observe {channel}")
        if self.sequencer.reducer_of(channel) != "chat":
            raise errors.invalid_params(f"{channel} is not a chat channel")
        if params.get("cursor") is not None:
            raise errors.invalid_params("unrecognised turns cursor")

        await self.sequencer.publish(channel, {"type": "chat/turnsLoaded", "turns": []})
        return {}

    # ─── session configuration ───────────────────────────────────────────

    def _config_request(self, params: Mapping[str, Any]) -> ConfigRequest:
        values = params.get("config")
        working_directory = params.get("workingDirectory")
        provider = params.get("provider")
        query = params.get("query")
        prop = params.get("property")
        return ConfigRequest(
            provider=provider if isinstance(provider, str) else None,
            working_directory=working_directory if isinstance(working_directory, str) else None,
            values=values if isinstance(values, Mapping) else {},
            property=prop if isinstance(prop, str) else None,
            query=query if isinstance(query, str) else "",
        )

    async def _resolve_session_config(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """What a session can be configured with, given what the client has chosen.

        Called repeatedly while the user sets a session up -- once per session in
        the measured VS Code trace -- and each answer is the **full** current
        property set, not a delta.

        A provider that does not implement `ConfiguresSessions` gets an empty
        schema rather than `MethodNotFound`. "This agent has nothing to
        configure" is a real answer; a refusal is indistinguishable from a
        broken host.
        """
        if not isinstance(self.provider, ConfiguresSessions):
            return {"schema": {"type": "object", "properties": {}}, "values": {}}

        resolved = await self.provider.resolve_config(self._config_request(params))
        schema: dict[str, Any] = {
            "type": "object",
            "properties": dict(resolved.properties),
        }
        if resolved.required:
            schema["required"] = list(resolved.required)
        return {"schema": schema, "values": dict(resolved.values)}

    async def _session_config_completions(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Values for a property whose schema declared `enumDynamic`.

        Answered with an empty list rather than refused when the provider does
        not implement it: a client only asks for a property whose schema *it was
        given*, so the honest failure is "no matches", not "no such method".
        """
        request = self._config_request(params)
        if request.property is None:
            raise errors.invalid_params("property is required")
        if not isinstance(self.provider, ConfiguresSessions):
            return {"items": []}

        items = await self.provider.complete_config(request)
        return {
            "items": [
                {
                    "value": item.value,
                    "label": item.label,
                    **({"description": item.description} if item.description else {}),
                }
                for item in items
            ]
        }

    async def _session_config_for(self, params: Mapping[str, Any]) -> dict[str, Any] | None:
        """`SessionState.config` for a session about to be created.

        The client has already walked `resolveSessionConfig` and passes what it
        settled on as `createSession.config`; the schema comes from the provider
        so the two agree. Values are filtered through the schema for the same
        reason a `root/configChanged` is: a value with no property is invisible
        to a client and unsettable, so publishing it only misleads.
        """
        if not isinstance(self.provider, ConfiguresSessions):
            return None
        resolved = await self.provider.resolve_config(self._config_request(params))
        if not resolved.properties:
            return None
        chosen = params.get("config")
        values = dict(resolved.values)
        if isinstance(chosen, Mapping):
            values.update(
                {k: v for k, v in chosen.items() if isinstance(k, str) and k in resolved.properties}
            )
        return {
            "schema": {"type": "object", "properties": dict(resolved.properties)},
            "values": values,
        }

    def _validate_session_config(
        self, connection: Connection, channel: str, action: Mapping[str, Any]
    ) -> str | None:
        """Gate `session/configChanged`, which is client-dispatchable.

        Same shape as the root gate, against the schema this session was created
        with. `sessionMutable` is the protocol's own marker for "the user may
        change this after creation"; without it a property is a creation-time
        choice and changing it later would leave the agent configured one way
        and the state saying another.
        """
        state = self.sequencer.state_of(channel)
        config = state.get("config") if isinstance(state, Mapping) else None
        schema = config.get("schema") if isinstance(config, Mapping) else None
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        if not isinstance(properties, Mapping):
            return "this session publishes no configuration"

        values = action.get("config")
        if not isinstance(values, Mapping):
            return "config must be an object"
        for key, value in values.items():
            prop = properties.get(key) if isinstance(key, str) else None
            if not isinstance(prop, Mapping):
                return f"{key!r} is not a configurable property"
            if not prop.get("sessionMutable"):
                return f"{key!r} cannot be changed after the session is created"
            if not type_matches(prop, value):
                return f"{key!r} does not accept that value"
            if not self.policy.may_set_root_config(connection.info, key, value):
                return f"{key!r} rejected by policy"
        if action.get("replace"):
            return "replacing the whole config is not permitted"
        return None

    def _validate_root_config(
        self, connection: Connection, action: Mapping[str, Any]
    ) -> str | None:
        """Gate `root/configChanged`, which is client-dispatchable.

        The schema is the gate, not the policy: an unknown key, a read-only one,
        or a value of the wrong type is refused whatever policy the embedder
        supplied -- and permissive policies are the norm on loopback. Policy is
        asked last, for the decisions a schema cannot express.
        """
        if self.root_config is None:
            # No schema published, so nothing is configurable. The reducer would
            # drop this anyway; rejecting says so out loud.
            return "this host publishes no configuration"
        config = action.get("config")
        if not isinstance(config, Mapping):
            return "config must be an object"
        for key, value in config.items():
            if not isinstance(key, str):
                return "config keys must be strings"
            rejection = self.root_config.rejection(key, value)
            if rejection is not None:
                return rejection
            if not self.policy.may_set_root_config(connection.info, key, value):
                return f"{key!r} rejected by policy"
        # `replace: true` drops every key the action omits, including ones this
        # peer could not have set. Refused: a merge expresses every legitimate
        # intent, and this does not.
        if action.get("replace"):
            return "replacing the whole config is not permitted"
        return None

    # ─── active clients ──────────────────────────────────────────────────

    async def _retire_active_client(self, connection: Connection) -> None:
        """Drop a departed client from every session that listed it.

        "The server SHOULD automatically dispatch that removal when an active
        client disconnects." Without it the session keeps advertising tools
        nobody can execute, and a provider that picks one waits forever for a
        result from a socket that is gone.
        """
        client_id = connection.client_id
        if not client_id:
            return
        for session in list(self._sessions.values()):
            state = self.sequencer.state_of(session.uri)
            if not isinstance(state, Mapping):
                continue
            clients = state.get("activeClients")
            if not isinstance(clients, list):
                continue
            if not any(
                isinstance(entry, Mapping) and entry.get("clientId") == client_id
                for entry in clients
            ):
                continue
            await self.sequencer.publish(
                session.uri,
                {"type": "session/activeClientRemoved", "clientId": client_id},
            )

    # ─── working directories ─────────────────────────────────────────────

    def _multiroot(self) -> Mapping[str, Any] | None:
        """`AgentCapabilities.multipleWorkingDirectories`, or ``None``.

        Absent means "clients MUST NOT mutate a session's or chat's
        working-directory set and MUST NOT set more than one entry" -- a client
        MUST that only the host can actually enforce.
        """
        capability = self.provider.agent.capabilities.get("multipleWorkingDirectories")
        return capability if isinstance(capability, Mapping) else None

    def _admit_working_directories(
        self, connection: Connection, session: str, params: Mapping[str, Any]
    ) -> list[str]:
        """The directories `createSession` may seed, after capability and policy."""
        requested = [d for d in params.get("workingDirectories") or () if isinstance(d, str)]
        if self._multiroot() is None:
            # "Servers without that capability treat only the first entry as the
            # session's working directory and ignore the rest." Truncate rather
            # than refuse: the spec makes this the server's defined behaviour,
            # not an error.
            requested = requested[:1]
        return [
            directory
            for directory in requested
            if self.policy.may_grant_working_directory(connection.info, session, directory)
        ]

    def _validate_working_directory_action(
        self, connection: Connection, channel: str, action: Mapping[str, Any]
    ) -> str | None:
        """Enforce the multiroot MUSTs the reducers deliberately do not.

        Upstream is explicit that these live here: "the pure reducers apply these
        mutations verbatim ... the `immutablePrimary` guarantee therefore lives
        at the dispatch-validation / host-acceptance layer, not in the reducer".
        """
        multiroot = self._multiroot()
        if multiroot is None:
            return "this agent does not advertise multipleWorkingDirectories"

        directory = action.get("directory")
        if not isinstance(directory, str):
            return "directory must be a string"

        if action["type"] == "session/workingDirectorySet":
            if not self.policy.may_grant_working_directory(connection.info, channel, directory):
                return "rejected by policy"
            return None

        # session/workingDirectoryRemoved. "A host MAY decline to apply the
        # removal (e.g. the immutable primary at index 0), leaving the set
        # unchanged" -- declined loudly, so the client reverts its optimistic
        # prediction instead of showing a directory that is still in use.
        if multiroot.get("immutablePrimary"):
            state = self.sequencer.state_of(channel)
            existing = state.get("workingDirectories") if isinstance(state, Mapping) else None
            if isinstance(existing, list) and existing and existing[0] == directory:
                return "the primary working directory is immutable"
        return None

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
        working_directories = self._admit_working_directories(connection, channel, params)
        # Resolved BEFORE the channel is registered, which is forced by the
        # protocol rather than chosen: `session/configChanged` carries values
        # only, and the reducer no-ops entirely when `SessionState.config` is
        # absent. A schema that is not in the initial state can never be added.
        session_config = await self._session_config_for(params)
        chat_uri = f"ahp-chat:/{uuid.uuid4()}"
        created_at = now_iso()
        session = _Session(
            uri=channel,
            chat_uri=chat_uri,
            provider_id=provider_id,
            title="New Session",
            created_at=created_at,
        )
        token = params.get("progressToken")
        session.publisher = _Publisher(self, session, token if isinstance(token, str) else None)
        self._sessions[channel] = session

        session_state: dict[str, Any] = {
            "provider": provider_id,
            "title": session.title,
            "status": _STATUS_IDLE,
            "lifecycle": "creating",
            # `createSession.activeClient` is how a client publishes the tools
            # it can execute on the agent's behalf. VS Code sends its ENTIRE
            # tool set here, with full input schemas (experiments.md E12e), and
            # a host that drops it is throwing away the one tool surface that
            # needs no filesystem API at all.
            "activeClients": _active_clients(params.get("activeClient")),
            "chats": [],
        }
        if working_directories:
            # Seeded rather than left absent: a chat subset "MUST already be in
            # the session's `workingDirectories`", and that check is unanswerable
            # against a set the host never recorded.
            session_state["workingDirectories"] = working_directories
        if session_config is not None:
            session_state["config"] = session_config
        await self.sequencer.register_channel(channel, session_state, "session")
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
        # "Each session owns at most one annotations channel. The channel URI is
        # derived from the session URI by appending `/annotations`."
        #
        # Registered even though this host produces no annotations, because a
        # client subscribing to an unregistered channel gets a result with no
        # `snapshot`, and VS Code's client THROWS on that rather than tolerating
        # it (`remoteAgentHostProtocolClient.ts:863-869`). It subscribes
        # unconditionally: the committed reconnect capture in
        # `tests/integration/fixtures/vscode-1.131-client-requests.json` carries
        # the annotations URI in `subscriptions`, so before this the failure
        # happened on every single connect.
        await self.sequencer.register_channel(
            session.annotations_uri, {"annotations": []}, "annotations"
        )

        # Bring-up runs after the response so the client can subscribe first.
        # Hold a reference: a bare create_task can be garbage-collected mid-flight.
        task = asyncio.create_task(self._bring_up(session, params))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return

    async def _bring_up(self, session: _Session, params: Mapping[str, Any]) -> None:
        try:
            active_client = _active_clients(params.get("activeClient"))
            context = AgentSessionContext(
                publisher=session.publisher,
                session_uri=session.uri,
                chat_uri=session.chat_uri,
                provider_id=session.provider_id,
                working_directories=tuple(params.get("workingDirectories") or ()),
                config=params.get("config") or {},
                active_client_id=active_client[0]["clientId"] if active_client else None,
                client_tools=tuple(active_client[0]["tools"]) if active_client else (),
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

        summary = self._full_summary(session)
        # Remember what root was told, so the first `root/sessionSummaryChanged`
        # carries a real difference rather than restating the whole summary.
        session.published_summary = self._project_summary(session)
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
        # Bring-up published customizations, tools and the chat catalogue, any of
        # which can move a summary field.
        await self._mirror_summary(session)

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
        await self.sequencer.drop_channel(session.annotations_uri)
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
        session = self._session_for(channel)
        if session is not None:
            await self._mirror_summary(session)

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

        if action_type == "root/configChanged":
            return self._validate_root_config(connection, action)

        if action_type == "session/configChanged":
            return self._validate_session_config(connection, channel, action)

        if action_type in _WORKING_DIRECTORY_ACTIONS:
            return self._validate_working_directory_action(connection, channel, action)

        # Asked of the bound reducer, never of the URI's scheme (invariant 15).
        # Classifying by scheme happens to work for chat URIs this host mints and
        # breaks the moment a client names one, which `createChat` will allow.
        state = self.sequencer.state_of(channel)
        if self.sequencer.reducer_of(channel) == "chat" and isinstance(state, Mapping):
            if action_type == "chat/turnCancelled" and state.get("activeTurn") is None:
                return "no active turn to cancel"
            if action_type == "chat/turnStarted" and state.get("activeTurn") is not None:
                return "a turn is already active"
            if (
                action_type in _TOOL_RESOLVING_ACTIONS
                and self.pending.id_for_key(action.get("toolCallId")) is None
            ):
                # A tool call the host is not waiting on. Rejected rather than
                # ignored, so the client reverts its optimistic state instead of
                # rendering a call as answered forever.
                return "no tool call awaiting that id"
            if action_type in _INPUT_ACTIONS and not self.pending.is_open(action.get("requestId")):
                # "Servers SHOULD reject client-dispatched input actions when no
                # unresolved input-request part has the matching requestId."
                # The reducers deliberately do not check this -- upstream states
                # the rule in prose and leaves it to the host -- and without it a
                # peer can answer a request that was never asked, or answer one
                # twice and resolve a future the second time round.
                return "no open input request with that id"
        return None

    async def _react(self, channel: str, action: Mapping[str, Any]) -> None:
        """Side effects a client action triggers on the agent."""
        action_type = action.get("type")
        if action_type == "session/customizationToggled":
            await self._react_to_toggle(channel, action)
            return
        session = next((s for s in self._sessions.values() if s.chat_uri == channel), None)
        if session is None:
            return
        if action_type == "chat/turnStarted":
            runner = TurnRunner(self.sequencer, channel, self.pending, session.uri)
            session.turn = asyncio.create_task(self._run_turn(session, runner, action))
        elif action_type == "chat/turnCancelled" and session.turn is not None:
            session.turn.cancel()
            if session.agent_session is not None:
                await session.agent_session.cancel("client cancelled")
        elif action_type == "chat/inputCompleted":
            # Resolved AFTER the reducer has applied the action (ADR 0005), so
            # the provider and the state every client can see never disagree
            # about whether the request was answered.
            request_id = action.get("requestId")
            if isinstance(request_id, str):
                response = action.get("response")
                resolved = self.pending.resolve(
                    request_id,
                    RequestOutcome(
                        response=response if isinstance(response, str) else "cancel",
                        # Read back out of state rather than off the action. The
                        # reducer has already overlaid the action's `answers`
                        # onto the drafts clients synchronised while the request
                        # was open -- "a user can answer one question on client A
                        # and another on client B" -- so the part now holds the
                        # merged result and re-deriving it here could only get it
                        # wrong.
                        payload=_answers_of(self.sequencer.state_of(channel), request_id),
                    ),
                )
                if resolved:
                    await self._retract_input_needed(session, request_id)
        elif action_type in _TOOL_RESOLVING_ACTIONS:
            request_id = self.pending.id_for_key(action.get("toolCallId"))
            if request_id is not None:
                if action_type == "chat/toolCallConfirmed":
                    approved = action.get("approved")
                    outcome = RequestOutcome(
                        response="accept" if approved is not False else "decline",
                        # `editedToolInput` is the client's rewrite of the
                        # parameters. Carried through so the provider runs what
                        # was approved rather than what it proposed.
                        payload={"toolInput": action["editedToolInput"]}
                        if "editedToolInput" in action
                        else {},
                    )
                else:
                    outcome = RequestOutcome(response="accept", payload=action.get("result"))
                if self.pending.resolve(request_id, outcome):
                    await self._retract_input_needed(session, request_id)

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Run something to completion outside the caller's cancellation scope.

        A bare `create_task` can be garbage-collected mid-flight, so the
        reference is held until it finishes.
        """
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _retract_input_needed(self, session: _Session, request_id: str) -> None:
        await self.sequencer.publish(
            session.uri, {"type": "session/inputNeededRemoved", "id": request_id}
        )
        await self._mirror_summary(session)

    async def _retract_all(self, session: _Session, request_ids: Sequence[str]) -> None:
        for request_id in request_ids:
            with contextlib.suppress(Exception):
                await self._retract_input_needed(session, request_id)

    async def _react_to_toggle(self, channel: str, action: Mapping[str, Any]) -> None:
        """Tell the provider a customization was switched on or off.

        The reducer has already flipped `enabled` in state, so clients agree
        without this. What they cannot do is stop the *agent* using a disabled
        skill -- only the provider can, and only if it is told.
        """
        session = self._sessions.get(channel)
        if session is None or not isinstance(session.agent_session, HandlesCustomizations):
            return
        customization_id = action.get("id")
        if not isinstance(customization_id, str):
            return
        with contextlib.suppress(Exception):
            await session.agent_session.customization_toggled(
                customization_id, bool(action.get("enabled"))
            )

    async def _run_turn(
        self, session: _Session, runner: TurnRunner, action: Mapping[str, Any]
    ) -> None:
        """Run one turn, then bring the root catalogue back in step.

        Deliberately mirrored at the two ends of the turn rather than per action.
        The spec sanctions exactly this: servers "MAY coalesce or debounce
        updates for noisy fields (for example, `modifiedAt` bumps while a turn is
        streaming)". Emitting per delta would be one root notification per token,
        to every connected client, to move a timestamp nobody is watching.
        """
        try:
            await runner.run(session.agent_session, action)
        finally:
            # Retracted in a DETACHED task on purpose. This one is frequently
            # the task being cancelled, and a cancelled task cannot be relied on
            # to finish another await -- but a session left advertising input
            # nobody can answer stays `InputNeeded` until it is disposed.
            if runner.abandoned:
                self._spawn(self._retract_all(session, [r.id for r in runner.abandoned]))
            with contextlib.suppress(Exception):
                await self._mirror_summary(session)

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
