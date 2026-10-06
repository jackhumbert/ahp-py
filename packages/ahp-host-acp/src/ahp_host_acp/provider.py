"""The adapter: one ACP agent process per AHP session, one ACP session per chat.

Each session spawns the configured agent command (`openclaw acp`, or any
other ACP agent) and opens an ACP session in it for each of its chats -- the
default chat, and every chat a client creates (`HostsChats`) -- so each chat
is its own conversation. Per chat it translates:

- `session/prompt` <- a user turn, its attachments and attached chats as the
  agent's prompt blocks allow (:mod:`ahp_host_acp.prompts`); its streamed
  `session/update`s become the host's neutral `TurnSink` events (text,
  reasoning, tool calls, usage);
- `session/request_permission` -> the host's `confirm_tool_call`, with the
  agent's own options (:mod:`ahp_host_acp.permissions`);
- a stop -> `session/cancel` for that chat's session (`CancelsChats`).

Out of turn, through the session's `SessionPublisher`:

- the agent's config options and modes are the session's config, both ways
  (:mod:`ahp_host_acp.options`); the title the default chat's session
  reports (`session_info_update`) is the session's title; the files tool
  calls edit are the session's changeset (:mod:`ahp_host_acp.changes`);
- slash commands are offered as completions (:mod:`ahp_host_acp.commands`),
  and a plan is shown as a row per update (:mod:`ahp_host_acp.plan`).

What follows the chat and what stays the session's: a chat has its own ACP
session, model, commands, plan, usage and turns in flight. The session config
is one set of values for the whole session (AHP has no per-chat config), kept
in every chat's ACP session; the title comes from the default chat; the
changeset covers every chat's edits, since they are edits to the same files.

What this adapter cannot do is decide *which* calls need approval: the agent
asks, or it does not. See the README's Security section.

Models: an agent with a `model` session config option (opencode) is switched
with `session/set_config_option`; one that reports ACP session models (an API
ACP has since removed), with `session/set_model`. One that does neither
(OpenClaw's bridge) can be given a `model_command`, a prompt template such as
``/model {model} -s`` sent as its own turn whenever the picked model changes;
its reply is not shown.

Sessions are resumable: each chat's ACP session id is the resume state,
restored with `session/resume` (or `session/load`) when the agent offers it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import uuid
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from ahp_host import AhpError
from ahp_host.provider.base import (
    AgentInfo,
    AgentInfoChanged,
    AgentSessionContext,
    ChatContext,
    CompletionItem,
    CompletionRequest,
    ConfigRequest,
    ConfigResolution,
    ConfigValue,
    ForkedFrom,
    ModelInfo,
    ToolConfirmation,
    TurnSink,
    UserMessage,
)

from ahp_host_acp import commands as slash
from ahp_host_acp import jsonrpc, permissions, prompts
from ahp_host_acp import plan as plans
from ahp_host_acp.catalogue import Catalogue
from ahp_host_acp.changes import Diff, SessionEdits, diffs_of
from ahp_host_acp.jsonrpc import (
    AcpConnection,
    AcpError,
    AgentExitedError,
    MethodNotFoundError,
)
from ahp_host_acp.mcp import McpServer
from ahp_host_acp.options import AgentOptions, coerce, same
from ahp_host_acp.paths import directory_of
from ahp_host_acp.roots import Roots, as_roots
from ahp_host_acp.tools import ToolCall, text_of

log = logging.getLogger(__name__)

PROTOCOL_VERSION: Final = 1
PROVIDER_ID: Final = "acp"
DEFAULT_DESCRIPTION: Final = "An Agent Client Protocol agent, running on this machine."
#: The file, in a host's (or a node agent's) state directory, that keeps what
#: the agent reported about itself: see :mod:`ahp_host_acp.catalogue`.
CATALOGUE_FILE: Final = "agent.json"
_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
#: How long a new turn waits for a stopped turn's prompt to wind down, so the
#: old turn's trailing updates never land in the new one.
SETTLE_TIMEOUT: Final = 15.0
#: `PermissionDenied`: what this host will not do (`ahp_protocol` error table).
PERMISSION_DENIED: Final = -32009

AgentChanged = Callable[[], Awaitable[None]]


def is_valid_provider_id(value: str) -> bool:
    """A provider id is how `createSession` names an agent; keep it plain."""
    return _PROVIDER_ID.fullmatch(value) is not None


class Connector(Protocol):
    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None,
        on_notification: jsonrpc.NotificationHandler,
        on_request: jsonrpc.RequestHandler,
    ) -> AcpConnection: ...


@dataclass(frozen=True)
class AgentSpec:
    """How to start the agent, and how to talk to it about models."""

    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    #: A prompt that switches the agent's model, `{model}` standing for the
    #: picked id; used only when the agent has no ACP model support.
    model_command: str | None = None
    #: ACP session config options fixed on every session it opens, by id:
    #: OpenClaw's `thought_level`, for one. A client sees them, read-only.
    config_options: Mapping[str, str | bool] = field(default_factory=dict)
    #: MCP servers the agent is given on every session.
    mcp_servers: Sequence[McpServer] = ()


class _Chat:
    """One AHP chat's own ACP session, in its AHP session's agent process."""

    def __init__(
        self,
        uri: str,
        *,
        acp_session_id: str | None = None,
        model: str | None = None,
        turns: int | None = 0,
    ) -> None:
        self.uri = uri
        self.acp_session_id = acp_session_id
        #: The agent process this chat's session is open in; see
        #: `AcpSession._generation`. 0: none yet.
        self.generation = 0
        #: The model the user wants, and the one the agent was last switched to.
        self.model = model
        self.applied_model: str | None = None
        self.native_models = False
        #: What this session reported it can be configured with; `verified`
        #: says whether the current agent process has reported its values.
        self.options = AgentOptions()
        self.verified = False
        #: What has been sent to this session in the current process, per option.
        self.applied: dict[str, Any] = {}
        #: While non-zero, its values are not republished: they are about to
        #: be overwritten with the session's own.
        self.hold = 0
        self.config_lock = asyncio.Lock()
        #: Held for a whole turn: one turn at a time per chat.
        self.lock = asyncio.Lock()
        self.sink: TurnSink | None = None
        #: While true, updates are dropped: a model switch's reply, or history
        #: an agent replays on `session/load`.
        self.quiet = False
        self.calls: dict[str, ToolCall] = {}
        self.announced: set[str] = set()
        #: The last diffs each running call showed: its final update may
        #: replace them with a plain result.
        self.diffs: dict[str, list[Diff]] = {}
        self.context_used: int | None = None
        self.context_size: int | None = None
        self.cost: Mapping[str, Any] | None = None
        self.inflight: asyncio.Task[Any] | None = None
        #: The agent's slash commands, once this session reports them.
        self.commands: tuple[slash.Command, ...] | None = None
        self.plan: tuple[plans.Entry, ...] | None = None
        #: How many AHP turns this conversation has been given, so a fork can
        #: tell whether ACP's whole-session fork copies exactly the turns the
        #: forked chat shows. ``None``: not known (a lost or replaced agent
        #: session, a state saved before this was kept).
        self.turns = turns

    @classmethod
    def restored(cls, uri: str, state: Mapping[str, Any], model: str | None) -> _Chat:
        session_id, saved_model, turns = (
            state.get("acpSessionId"),
            state.get("model"),
            state.get("turns"),
        )
        return cls(
            uri,
            acp_session_id=session_id if isinstance(session_id, str) else None,
            model=saved_model if isinstance(saved_model, str) else model,
            turns=turns if isinstance(turns, int) and not isinstance(turns, bool) else None,
        )

    def state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if self.acp_session_id:
            state["acpSessionId"] = self.acp_session_id
        if self.model:
            state["model"] = self.model
        if self.turns is not None:
            state["turns"] = self.turns
        return state

    @property
    def model_option(self) -> str | None:
        """The id of the agent's model config option (`category: "model"`), if any."""
        option = self.options.model_option
        return option.id if option is not None else None


async def _nothing() -> None:
    return None


class AcpSession:
    def __init__(
        self,
        context: AgentSessionContext,
        *,
        roots: Path | Roots,
        spec: AgentSpec,
        connect: Connector,
        default_model: str | None = None,
        acp_session_id: str | None = None,
        directory: Path | None = None,
        catalogue: Catalogue | None = None,
        config_properties: Mapping[str, Mapping[str, Any]] | None = None,
        on_agent_changed: AgentChanged | None = None,
        turns: int | None = 0,
        chats: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.context = context
        self._roots = as_roots(roots)
        self._spec = spec
        self._connect = connect
        #: Set on resume: the folder the session was started in.
        self.directory = directory
        self._default = _Chat(
            context.chat_uri,
            acp_session_id=acp_session_id,
            model=context.model or default_model,
            turns=turns,
        )
        self._chats: dict[str, _Chat] = {context.chat_uri: self._default}
        #: Restored chats' saved state, until `chat_opened` announces them.
        self._saved_chats: dict[str, Mapping[str, Any]] = {
            uri: state for uri, state in (chats or {}).items() if isinstance(state, Mapping)
        }
        self._conn: AcpConnection | None = None
        #: Counts agent processes: a chat whose `generation` differs has no
        #: session open in the current one.
        self._generation = 0
        self._process_lock = asyncio.Lock()
        #: One session opened at a time, so an update for a session id not
        #: known yet can only belong to `_opening`.
        self._open_lock = asyncio.Lock()
        self._opening: _Chat | None = None
        self._capabilities: Mapping[str, Any] = {}
        self._catalogue = catalogue if catalogue is not None else Catalogue()
        self._on_agent_changed = on_agent_changed or _nothing

        # -- session config ----------------------------------------------------
        #: The session's config values, for the properties its schema has --
        #: the provider's schema at creation, or the restored state's keys.
        #: A mirror of the session's state: at creation the host keeps the
        #: client's value for any property the schema has, read-only and
        #: invalid ones included, so they are taken as given here too, and a
        #: value the agent does not end up with is corrected from its report.
        if config_properties is not None:
            self._properties = set(config_properties)
            self._config: dict[str, Any] = {
                key: prop["default"] for key, prop in config_properties.items() if "default" in prop
            }
            self._config.update(
                (key, value) for key, value in context.config.items() if key in config_properties
            )
        else:
            self._properties = set(context.config)
            self._config = dict(context.config)

        self._title: str | None = None
        self._edits = SessionEdits(self._roots)
        #: A forked session's copied transcript, until the agent holds it: the
        #: fork's ACP session reopened, or it given as context (`fork_from`).
        self._seed: ForkedFrom | None = (
            context.fork if context.fork is not None and context.fork.turns else None
        )
        if self._seed is not None:
            self._default.turns = None  # until the fork is made

    # -- the default chat, as callers have always seen it -------------------------

    @property
    def acp_session_id(self) -> str | None:
        """The default chat's ACP session id."""
        return self._default.acp_session_id

    @property
    def model(self) -> str | None:
        return self._default.model

    @model.setter
    def model(self, value: str | None) -> None:
        self._default.model = value

    @property
    def commands(self) -> tuple[slash.Command, ...] | None:
        """The default chat's slash commands, once its agent session reports them."""
        return self._default.commands

    def hosts(self, chat_uri: str) -> bool:
        """Whether *chat_uri* is one of this session's chats."""
        return chat_uri in self._chats or chat_uri in self._saved_chats

    def commands_for(self, chat_uri: str) -> tuple[slash.Command, ...] | None:
        """*chat_uri*'s commands, else the default chat's."""
        chat = self._chats.get(chat_uri)
        if chat is not None and chat.commands is not None:
            return chat.commands
        return self._default.commands

    # -- lifecycle -----------------------------------------------------------

    def working_directory(self) -> Path:
        """The session's directory, which must lie inside a served folder."""
        if self.directory is not None:
            resolved = self.directory.resolve()
            if not self._roots.contains(resolved):
                raise PermissionError(f"{resolved} is outside this host's folders")
            return resolved
        for uri in self.context.working_directories:
            real = self._roots.real_path(uri)
            if real is not None:
                return real
            path = directory_of(uri)
            if path is not None:
                raise PermissionError(f"{path.resolve()} is outside this host's folders")
        return self._roots.primary

    async def _ensure_process(self) -> AcpConnection:
        """The agent process, started and initialized if it is not running."""
        async with self._process_lock:
            if self._conn is not None and not self._conn.closed:
                return self._conn
            if self._conn is not None:
                await self._drop_connection()
            cwd = self.working_directory()
            self.directory = cwd
            conn = await self._connect(
                self._spec.command,
                cwd=cwd,
                env=self._spec.env,
                on_notification=self._on_notification,
                on_request=self._on_request,
            )
            try:
                init = await conn.request(
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        # No client filesystem or terminal: the agent works with
                        # its own tools, and every host-side read goes through
                        # the jail.
                        "clientCapabilities": {
                            "fs": {"readTextFile": False, "writeTextFile": False},
                            "terminal": False,
                        },
                        "clientInfo": {"name": "ahp-host-acp", "version": _version()},
                    },
                )
            except BaseException:
                await conn.aclose()
                raise
            self._capabilities = _mapping(_mapping(init).get("agentCapabilities"))
            self._generation += 1
            self._conn = conn
        if self._catalogue.remember_capabilities(self._capabilities):
            await self._on_agent_changed()  # whether it can fork may have changed
        return conn

    async def _ensure(self, chat: _Chat) -> AcpConnection:
        """The agent process, with *chat*'s session open in it."""
        conn = await self._ensure_process()
        if chat.generation == self._generation:
            return conn
        async with self._open_lock:
            if chat.generation != self._generation:
                chat.hold += 1
                try:
                    chat.applied = {}
                    chat.verified = False
                    await self._open(conn, chat)
                    chat.generation = self._generation
                    await self._sync_config(conn, chat)
                finally:
                    chat.hold -= 1
        await self._publish_config(chat)
        return conn

    def _session_capability(self, name: str) -> bool:
        session = _mapping(self._capabilities.get("sessionCapabilities"))
        return isinstance(session.get(name), Mapping)

    def _mcp_servers(self) -> list[dict[str, Any]]:
        servers = (server.to_acp(self._capabilities) for server in self._spec.mcp_servers)
        return [server for server in servers if server is not None]

    def _base(self) -> dict[str, Any]:
        return {"cwd": str(self.directory), "mcpServers": self._mcp_servers()}

    async def _open(self, conn: AcpConnection, chat: _Chat) -> None:
        """Open *chat*'s session in this process: reattach it, or start one."""
        base = self._base()
        self._opening = chat
        try:
            if chat.acp_session_id is not None:
                if await self._reattach(conn, chat, base):
                    if chat is self._default:
                        self._seed = None  # the fork: the agent has the conversation
                    return
                log.warning(
                    "could not reopen ACP session %s; starting a new one", chat.acp_session_id
                )
                # The chat shows turns the new session never saw.
                chat.turns = None
            result = _mapping(await conn.request("session/new", base))
            session_id = result.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise AcpError("the agent's session/new returned no sessionId")
            chat.acp_session_id = session_id
        finally:
            self._opening = None
        self._note_session(chat, result)
        self._catalogue.remember_new_session(result)
        await self._on_agent_changed()
        chat.applied_model = None

    async def _reattach(self, conn: AcpConnection, chat: _Chat, base: Mapping[str, Any]) -> bool:
        params = {**base, "sessionId": chat.acp_session_id}
        try:
            if self._session_capability("resume"):
                result = await conn.request("session/resume", params)
            elif self._capabilities.get("loadSession"):
                # `session/load` replays the conversation as updates; the
                # client already shows it, so the replay is not published.
                chat.quiet = True
                try:
                    result = await conn.request("session/load", params)
                finally:
                    chat.quiet = False
            else:
                return False
        except AcpError as exc:
            log.warning("reopening ACP session %s failed: %s", chat.acp_session_id, exc)
            return False
        self._note_session(chat, _mapping(result))
        # The agent's own model may have survived, but not provably: switch again.
        chat.applied_model = None
        return True

    def _note_session(self, chat: _Chat, result: Mapping[str, Any]) -> None:
        """What a session/new, resume, load or fork answer says about configuration.

        Newer agents (opencode) offer the model as a session config option of
        category `model`; older ones report `models` for `session/set_model`.
        An answer that says neither (some agents' `session/resume`) keeps what
        was known about the options' shape, but not about their values: this
        agent process has not said what they are.
        """
        if "models" in result:
            chat.native_models = bool(_mapping(result.get("models")).get("availableModels"))
        chat.options.note(result)
        if _reports_options(result):
            chat.verified = True

    async def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.aclose()
            except Exception:  # a dying agent must not fail session disposal
                log.exception("closing the ACP agent failed")

    async def aclose(self) -> None:
        await self._drop_connection()

    def _live(self, chat: _Chat) -> AcpConnection | None:
        """The connection *chat*'s session is open on, if it is open now."""
        conn = self._conn
        if conn is None or conn.closed or chat.acp_session_id is None:
            return None
        return conn if chat.generation == self._generation else None

    async def _cancel(self, chat: _Chat) -> None:
        conn = self._live(chat)
        if conn is not None:
            try:
                await conn.notify("session/cancel", {"sessionId": chat.acp_session_id})
            except AcpError:
                log.debug("session/cancel did not reach the agent", exc_info=True)

    async def cancel(self, reason: str | None = None) -> None:
        """Every chat's turn: the whole session is being stopped."""
        for chat in list(self._chats.values()):
            await self._cancel(chat)

    async def cancel_chat(self, chat_uri: str, reason: str | None = None) -> None:
        """`CancelsChats`: stop one chat's turn; the others keep going."""
        chat = self._chats.get(chat_uri)
        if chat is not None:
            await self._cancel(chat)

    # -- chats -----------------------------------------------------------------

    def _chat_for(self, chat_uri: str | None) -> _Chat:
        """The chat a turn is for. One never announced gets a session of its own."""
        if chat_uri is None:
            return self._default
        chat = self._chats.get(chat_uri)
        if chat is None:
            saved = self._saved_chats.pop(chat_uri, None)
            chat = (
                _Chat.restored(chat_uri, saved, self._default.model)
                if saved is not None
                else _Chat(chat_uri, model=self._default.model)
            )
            self._chats[chat_uri] = chat
        return chat

    async def chat_opened(self, context: ChatContext) -> None:
        """`HostsChats`: a chat beyond the default one, with its own ACP session.

        A new chat's session starts with its first turn, as the default chat's
        does. A restored one picks up the session it had. A fork is made at
        once, with ACP's `session/fork`, since the conversation it copies is
        the source's as it is now.
        """
        uri = context.chat_uri
        if uri in self._chats:
            return
        if context.origin_kind == "sideChat":
            # Not advertised: ACP has no way to give a session context that is
            # not part of its own conversation.
            raise AhpError(PERMISSION_DENIED, "this agent cannot open side chats")
        saved = self._saved_chats.pop(uri, None)
        if saved is not None:
            self._chats[uri] = _Chat.restored(uri, saved, self._default.model)
            return
        chat = _Chat(uri, model=self._default.model)
        if context.fork is not None:
            await self._fork(chat, context.fork)
        self._chats[uri] = chat

    async def _fork(self, chat: _Chat, fork: ForkedFrom) -> None:
        """Give *chat* a fork of its source chat's ACP session."""
        source, result = await self._forked(fork, into=chat)
        chat.acp_session_id = str(result["sessionId"])
        chat.generation = self._generation
        chat.model, chat.applied_model = source.model, source.applied_model
        chat.native_models = source.native_models
        chat.turns = len(fork.turns)
        self._note_session(chat, result)

    async def _forked(
        self,
        fork: ForkedFrom,
        *,
        into: _Chat | None = None,
        cwd: Path | None = None,
    ) -> tuple[_Chat, Mapping[str, Any]]:
        """`session/fork` of the chat *fork* names, in this session's agent process.

        ACP's `session/fork` (unstable, `sessionCapabilities.fork`) copies the
        source session whole: it takes no turn to branch at. An AHP fork
        copies the source chat through one turn -- so it is made only when
        that turn is the last one the source's agent session has seen, and
        the source is not mid-turn. Anything else would leave the agent
        remembering turns the fork does not show.

        *into* is the chat that will hold the fork here; without one the fork
        is for another AHP session (*cwd* its folder), whose own agent process
        must then reopen it -- so the agent must also offer `session/resume`
        or `session/load`.
        """
        source = self._chats.get(fork.chat_uri or self._default.uri)
        if source is None:
            raise AhpError(PERMISSION_DENIED, "the chat to fork is not this agent's")
        if source.lock.locked() or source.turns is None or source.turns != len(fork.turns):
            raise AhpError(
                PERMISSION_DENIED,
                "this agent forks a whole conversation, so a chat can only be forked "
                "at its latest turn, and not while it is answering",
            )
        async with source.lock:
            conn = await self._ensure(source)
            if not self._session_capability("fork"):
                raise AhpError(PERMISSION_DENIED, "this agent cannot fork a session")
            if into is None and not (
                self._session_capability("resume") or self._capabilities.get("loadSession")
            ):
                raise AhpError(PERMISSION_DENIED, "this agent cannot reopen a forked session")
            base = self._base() if cwd is None else {**self._base(), "cwd": str(cwd)}
            async with self._open_lock:
                # Without a chat here, an update for the fork's id is dropped.
                self._opening = into
                try:
                    result = _mapping(
                        await conn.request(
                            "session/fork", {**base, "sessionId": source.acp_session_id}
                        )
                    )
                finally:
                    self._opening = None
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpError("the agent's session/fork returned no sessionId")
        return source, result

    async def fork_out(self, fork: ForkedFrom, cwd: Path) -> str | None:
        """A fork of one of this session's chats, for a new session in *cwd*.

        The ACP session id of the fork, for the new session to reopen in its
        own agent process; ``None`` (logged) when this agent or this chat
        cannot be forked honestly.
        """
        try:
            _, result = await self._forked(fork, cwd=cwd)
        except (AhpError, AcpError, OSError, PermissionError) as exc:
            log.info("session fork from %s not made: %s", fork.session_uri, exc)
            return None
        return str(result["sessionId"])

    async def fork_from(self, source: AcpSession | None, fork: ForkedFrom) -> None:
        """`createSession.fork`: start as a fork of *source*'s conversation.

        With an agent that can fork (`session/fork`, then `session/resume` or
        `session/load` here) under the same rule as a chat fork, the default
        chat's ACP session is the fork. Otherwise -- a source that is not this
        agent's, or not running, or not at its latest turn -- the session
        starts fresh, and its first message carries the copied transcript as
        context, with a system notification saying so: the agent then knows
        what the user sees, and the user knows how. That context is not kept
        across a host restart before the first message.
        """
        if source is None:
            log.info("session fork from %s: no running session of this agent", fork.session_uri)
            return
        try:
            cwd = self.working_directory()
        except PermissionError:
            return
        session_id = await source.fork_out(fork, cwd)
        if session_id is not None:
            self._default.acp_session_id = session_id
            self._default.turns = len(fork.turns)

    async def chat_closed(self, chat_uri: str) -> None:
        """`HostsChats`: the chat is gone. Its ACP session is closed, if the
        agent can (`session/close`), and otherwise left to the agent."""
        self._saved_chats.pop(chat_uri, None)
        chat = self._chats.get(chat_uri)
        if chat is None or chat is self._default:
            return
        del self._chats[chat_uri]
        conn = self._live(chat)
        if conn is None:
            return
        try:
            if self._session_capability("close"):
                await conn.request("session/close", {"sessionId": chat.acp_session_id})
            elif chat.lock.locked():
                await conn.notify("session/cancel", {"sessionId": chat.acp_session_id})
        except AcpError as exc:
            log.debug("closing ACP session %s: %s", chat.acp_session_id, exc)

    def resume_state(self) -> dict[str, Any] | None:
        """What `resume_state_of` returns: the default chat's session at the
        top level (as before chats had their own), every other chat's under
        `chats`."""
        state: dict[str, Any] = {}
        if self._default.acp_session_id:
            state["acpSessionId"] = self._default.acp_session_id
        chats: dict[str, Any] = dict(self._saved_chats)
        for uri, chat in self._chats.items():
            if chat is not self._default and chat.acp_session_id:
                chats[uri] = chat.state()
        if not state and not chats:
            return None
        if self.directory is not None:
            state["cwd"] = str(self.directory)
        if self._default.model:
            state["model"] = self._default.model
        if self._default.turns is not None:
            state["turns"] = self._default.turns
        if chats:
            state["chats"] = chats
        return state

    # -- session config --------------------------------------------------------

    def _wanted(self) -> dict[str, Any]:
        """What each option should be: the config file's, else the session's."""
        pinned = self._spec.config_options
        wanted = {key: value for key, value in self._config.items() if key not in pinned}
        wanted.update(pinned)
        return wanted

    async def _sync_config(self, conn: AcpConnection, chat: _Chat) -> None:
        """Set every option *chat*'s session does not already have as wanted.

        Each value is sent once per agent process: a refusal is logged and not
        retried every turn, and an option the agent later changes itself is
        not changed back. A refused or ignored change is then undone in the
        session's state by :meth:`_publish_config`, from what the agent
        reports.
        """
        async with chat.config_lock:
            chat.hold += 1
            try:
                for key, value in self._wanted().items():
                    option = chat.options.get(key)
                    value = coerce(option, value) if option is not None else value
                    if chat.verified and option is not None and same(option.current, value):
                        chat.applied[key] = value
                        continue
                    if key in chat.applied and same(chat.applied[key], value):
                        continue
                    chat.applied[key] = value
                    method, params = chat.options.request_for(key, value)
                    try:
                        result = await conn.request(
                            method, {"sessionId": chat.acp_session_id, **params}
                        )
                    except AgentExitedError:
                        raise
                    except AcpError as exc:
                        log.warning("the agent refused %s=%s: %s", key, value, exc)
                        continue
                    answer = _mapping(result)
                    chat.options.note(answer)
                    if _reports_options(answer):
                        chat.verified = True
                    else:
                        chat.options.assume(key, value)
            finally:
                chat.hold -= 1
        await self._publish_config(chat)

    async def _publish_config(self, chat: _Chat) -> None:
        """Tell clients the values *chat*'s agent session reports, where they
        differ from the session's. The agent is what is actually in force.

        Only for an option the chat's session was given the value it should
        have: one that has not caught up with a change yet (another chat's,
        applied to it on its next turn) is not in force anywhere a client is
        looking, and publishing it would undo that change.
        """
        if chat.hold or not chat.verified:
            return
        wanted = self._wanted()
        changed: dict[str, Any] = {}
        for key in self._properties:
            option = chat.options.get(key)
            if option is None or option.current is None:
                continue
            if key not in wanted or key not in chat.applied:
                continue
            if not same(chat.applied[key], coerce(option, wanted[key])):
                continue
            reported = option.current
            if key not in self._config or not same(self._config[key], reported):
                changed[key] = reported
        if not changed:
            return
        self._config.update(changed)
        publisher = self.context.publisher
        if publisher is not None:
            await publisher.config_changed(changed)

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        """`ReconfiguresSessions`: a client changed a `sessionMutable` property.

        Applied straight away to every chat whose agent session is open (ACP
        lets a mode change mid-turn); to the others when they next start.
        """
        for key, value in values.items():
            if key in self._properties and key not in self._spec.config_options:
                self._config[key] = value
        for chat in list(self._chats.values()):
            conn = self._live(chat)
            if conn is None:
                continue
            try:
                await self._sync_config(conn, chat)
            except AcpError as exc:
                log.warning("could not reconfigure ACP session %s: %s", chat.acp_session_id, exc)

    # -- agent -> client -------------------------------------------------------

    def _chat_of(self, session_id: Any) -> _Chat | None:
        if isinstance(session_id, str):
            for chat in self._chats.values():
                if chat.acp_session_id == session_id:
                    return chat
            # One session is opened at a time: an update for a session this
            # process has just created, before its id is known, is that one's.
            if self._opening is not None:
                return self._opening
        return None

    async def _on_notification(self, method: str, params: Any) -> None:
        if method != "session/update":
            log.debug("ignoring ACP notification %s", method)
            return
        params = _mapping(params)
        chat = self._chat_of(params.get("sessionId"))
        if chat is None:
            return
        update = _mapping(params.get("update"))
        kind = update.get("sessionUpdate")
        # Session state first: it is true whether or not a turn is running,
        # and a `session/load` replay brings it up to date too.
        if kind == "usage_update":
            await self._on_usage(chat, update)
            return
        if kind in ("config_option_update", "current_mode_update"):
            chat.options.note_update(update)
            if kind == "config_option_update":
                chat.verified = True
            await self._publish_config(chat)
            return
        if kind == "available_commands_update":
            raw = update.get("availableCommands")
            chat.commands = slash.parse_commands(raw)
            self._catalogue.remember_commands(raw)
            return
        if kind == "session_info_update":
            # The session is named after its default chat's conversation; a
            # replayed title is history, not a rename.
            if chat is self._default and not chat.quiet:
                await self._on_title(update)
            return
        if kind == "plan":
            await self._on_plan(chat, update)
            return
        sink = chat.sink
        if sink is None or chat.quiet:
            return
        if kind == "agent_message_chunk":
            text = _text_block(update.get("content"))
            if text:
                await sink.text_delta(text)
        elif kind == "agent_thought_chunk":
            text = _text_block(update.get("content"))
            if text:
                await sink.reasoning_delta(text)
        elif kind in ("tool_call", "tool_call_update"):
            await self._on_tool_call(chat, update, sink)
        else:
            log.debug("ignoring session update %s", kind)

    async def _on_usage(self, chat: _Chat, update: Mapping[str, Any]) -> None:
        """`usage_update`: tokens in context, the window's size, and the cost.

        `used` stands in for a turn's input tokens when the prompt's answer
        has none; `size` and `cost` ride on the turn's usage `_meta` (see
        :meth:`_report_usage`), and `size` is remembered as the model's
        context window for the picker.
        """
        used, size, cost = update.get("used"), update.get("size"), update.get("cost")
        if isinstance(used, int) and not isinstance(used, bool):
            chat.context_used = used
        if isinstance(size, int) and not isinstance(size, bool) and size > 0:
            chat.context_size = size
            self._catalogue.remember_context_window(_current_model(chat), size)
            await self._on_agent_changed()
        if isinstance(cost, Mapping) and isinstance(cost.get("amount"), int | float):
            chat.cost = dict(cost)

    async def _on_title(self, update: Mapping[str, Any]) -> None:
        """`session_info_update.title` renames the session.

        A null title ("set to null to clear") is not passed on: AHP has no
        untitled state to go back to, and the session keeps the name it has.
        """
        title = update.get("title")
        publisher = self.context.publisher
        if not isinstance(title, str) or not title.strip() or title == self._title:
            return
        self._title = title
        if publisher is not None:
            await publisher.title_changed(title)

    async def _on_plan(self, chat: _Chat, update: Mapping[str, Any]) -> None:
        """A plan update: one finished "Update plan" row. See :mod:`ahp_host_acp.plan`."""
        entries = plans.parse_plan(update.get("entries"))
        if entries == chat.plan:
            return  # resent unchanged
        chat.plan = entries
        sink = chat.sink
        if sink is None or chat.quiet:
            return
        call_id = f"acp-plan-{uuid.uuid4().hex[:12]}"
        await sink.tool_call_started(
            call_id,
            plans.TOOL_NAME,
            {"entries": update.get("entries")},
            display_name=plans.DISPLAY_NAME,
        )
        await sink.tool_call_completed(
            call_id,
            {"content": [{"type": "text", "text": plans.markdown(entries)}]},
            success=True,
            past_tense_message=f"Updated the plan: {plans.progress(entries)}",
        )
        working = plans.current(entries)
        publisher = self.context.publisher
        if working is not None and publisher is not None:
            # After the row: the host clears the activity when a call completes.
            await publisher.activity_changed(working)

    async def _on_tool_call(self, chat: _Chat, update: Mapping[str, Any], sink: TurnSink) -> None:
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return
        call = chat.calls.setdefault(call_id, ToolCall(call_id))
        before = call.progress_line()
        call.merge(update)
        await self._announce(chat, call, sink)
        if call.finished:
            await self._complete(chat, call, sink)
            return
        if "content" in update:
            self._note_diffs(chat, call)
        if call.progress_line() != before:
            await sink.tool_call_delta(call_id, invocation_message=call.progress_line())
        if "content" in update and call.content:
            await sink.tool_call_output(call_id, text_of(call.content))

    async def _announce(self, chat: _Chat, call: ToolCall, sink: TurnSink) -> None:
        if call.call_id in chat.announced:
            return
        chat.announced.add(call.call_id)
        await sink.tool_call_started(
            call.call_id, call.name, call.raw_input, display_name=call.display_name
        )
        await sink.tool_call_delta(call.call_id, invocation_message=call.progress_line())

    async def _complete(self, chat: _Chat, call: ToolCall, sink: TurnSink) -> None:
        """Finish a call. A completed edit's result carries a `fileEdit` per
        file -- the diff a client renders in the row -- in place of the text
        summary of the same diff."""
        if chat.calls.pop(call.call_id, None) is None:
            return
        diffs = diffs_of(call.content) or chat.diffs.pop(call.call_id, [])
        chat.diffs.pop(call.call_id, None)
        edits: list[dict[str, Any]] = []
        shown: set[str] = set()
        changed = False
        if diffs and call.status == "completed":
            made, changed = self._edits.completed(diffs, self.directory)
            for diff, change in made:
                try:
                    edits.append(dict(await sink.file_edit(change)))
                except Exception:  # the text summary stays in its place
                    log.exception("could not store the edit to %s", change.uri)
                else:
                    shown.add(diff.path)
        elif diffs:
            self._edits.abandoned(diffs, self.directory)
        await sink.tool_call_completed(
            call.call_id,
            {"content": [*text_of(call.content, shown=shown), *edits]},
            success=call.status != "failed",
            past_tense_message=call.past_tense(),
        )
        if changed:
            await self._publish_changes()

    def _note_diffs(self, chat: _Chat, call: ToolCall) -> None:
        """A running call shows diffs: remember them, and the files before it runs."""
        diffs = diffs_of(call.content)
        if diffs:
            chat.diffs[call.call_id] = diffs
            self._edits.announced(diffs, self.directory)

    async def _publish_changes(self) -> None:
        publisher = self.context.publisher
        if publisher is None:
            return
        try:
            await publisher.changes_published(self._edits.changeset, self._edits.changes())
        except Exception:  # the edit happened; failing to show it must not fail the turn
            log.exception("publishing the session's changes failed")

    async def _on_request(self, method: str, params: Any) -> Any:
        if method == "session/request_permission":
            return await self._request_permission(_mapping(params))
        # fs/*, terminal/* were not offered in clientCapabilities.
        raise MethodNotFoundError(method)

    async def _request_permission(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Put the agent's question, and its own choices, to the user.

        The policy is :mod:`ahp_host_acp.permissions`: the user picks from the
        agent's options, *always* ones included, and nothing is ever picked
        for them. A call that shows a diff is previewed (`edits`), so the
        change can be read before it is allowed.
        """
        chat = self._chat_of(params.get("sessionId"))
        sink = chat.sink if chat is not None else None
        if chat is None or sink is None:
            return permissions.CANCELLED
        update = _mapping(params.get("toolCall"))
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return permissions.CANCELLED
        offered = permissions.acp_options(params.get("options"))
        call = chat.calls.setdefault(call_id, ToolCall(call_id))
        call.merge(update)
        self._note_diffs(chat, call)
        await self._announce(chat, call, sink)
        try:
            outcome = await sink.confirm_tool_call(
                ToolConfirmation(
                    call_id=call_id,
                    name=call.name,
                    display_name=call.display_name,
                    invocation_message=call.approval_line(),
                    tool_input=call.raw_input,
                    confirmation_title=call.title or None,
                    options=permissions.confirmation_options(offered),
                    edits=self._edits.preview(diffs_of(call.content), self.directory),
                )
            )
        except asyncio.CancelledError:
            return permissions.CANCELLED  # the turn ended while the prompt was open
        return permissions.answer(offered, outcome)

    # -- turns ---------------------------------------------------------------

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        chat = self._chat_for(message.chat_uri)
        async with chat.lock:
            await self._settle(chat)
            if message.model is not None:
                chat.model = message.model.id
            try:
                conn = await self._ensure(chat)
                await self._sync_config(conn, chat)
                await self._apply_model(conn, chat)
            except PermissionError as error:
                await sink.turn_failed(str(error), error_type="agent.workingDirectory")
                return
            except (AcpError, OSError) as error:
                if isinstance(error, AgentExitedError):
                    await self._drop_connection()
                await sink.turn_failed(f"Could not start the agent: {error}", "acp.start")
                return
            chat.sink = sink
            chat.calls.clear()
            chat.announced.clear()
            chat.diffs.clear()
            if chat.turns is not None:
                chat.turns += 1
            try:
                await self._run_turn(conn, chat, message, sink)
            finally:
                chat.sink = None

    async def _settle(self, chat: _Chat) -> None:
        inflight, chat.inflight = chat.inflight, None
        if inflight is not None and not inflight.done():
            await asyncio.wait({inflight}, timeout=SETTLE_TIMEOUT)

    async def _prompt(
        self, conn: AcpConnection, chat: _Chat, blocks: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        """One `session/prompt`, shielded: a stopped turn lets it finish in the background."""
        task = asyncio.create_task(
            conn.request(
                "session/prompt", {"sessionId": chat.acp_session_id, "prompt": list(blocks)}
            )
        )
        chat.inflight = task
        task.add_done_callback(_consume)
        return _mapping(await asyncio.shield(task))

    async def _apply_model(self, conn: AcpConnection, chat: _Chat) -> None:
        wanted = chat.model
        if not wanted or wanted == chat.applied_model:
            return
        if chat.model_option is not None:
            result = await conn.request(
                "session/set_config_option",
                {"sessionId": chat.acp_session_id, "configId": chat.model_option, "value": wanted},
            )
            answer = _mapping(result)
            chat.options.note(answer)
            if _reports_options(answer):
                chat.verified = True
        elif chat.native_models:
            await conn.request(
                "session/set_model", {"sessionId": chat.acp_session_id, "modelId": wanted}
            )
        elif self._spec.model_command:
            chat.quiet = True
            try:
                result = await self._prompt(
                    conn, chat, [prompts.text(self._spec.model_command.format(model=wanted))]
                )
            finally:
                chat.quiet = False
            log.info(
                "switched session %s to %s (%s)",
                chat.acp_session_id,
                wanted,
                result.get("stopReason"),
            )
        chat.applied_model = wanted

    async def _run_turn(
        self, conn: AcpConnection, chat: _Chat, message: UserMessage, sink: TurnSink
    ) -> None:
        chat.context_used = None
        builder = prompts.PromptBuilder(
            self._roots, _mapping(self._capabilities.get("promptCapabilities"))
        )
        blocks = builder.blocks(message)
        seed, self._seed = (self._seed, None) if chat is self._default else (None, self._seed)
        if seed is not None:
            # After the message, like any other context: the message comes first.
            blocks.insert(
                1,
                prompts.chat_block(
                    seed.chat_uri or seed.session_uri,
                    "The conversation this session was forked from",
                    seed.turns,
                    builder.embedded,
                ),
            )
            await sink.system_notification(
                "This agent could not take the forked conversation over itself, so the "
                "earlier turns were given to it as context with this message."
            )
        try:
            result = await self._prompt(conn, chat, blocks)
        except asyncio.CancelledError:
            await self._cancel(chat)
            raise
        except AgentExitedError as error:
            await self._close_open_calls(chat, sink)
            await self._drop_connection()
            chat.turns = None  # whether the agent kept this turn is anyone's guess
            await sink.turn_failed(str(error), error_type="acp.agentExited")
            return
        except AcpError as error:
            await self._close_open_calls(chat, sink)
            await sink.turn_failed(str(error), error_type=f"acp.{error.code}")
            return
        await self._close_open_calls(chat, sink)
        await self._report_usage(chat, result, sink)
        stop = result.get("stopReason")
        if stop in ("end_turn", "cancelled", None):
            return
        reasons = {
            "max_tokens": "The agent hit its output token limit.",
            "max_turn_requests": "The agent hit its limit of model requests for one turn.",
            "refusal": "The agent refused to continue.",
        }
        await sink.turn_failed(reasons.get(str(stop), f"The agent stopped: {stop}"), f"acp.{stop}")

    async def _close_open_calls(self, chat: _Chat, sink: TurnSink) -> None:
        """A call the agent never finished is shown as failed, not left spinning."""
        for call in list(chat.calls.values()):
            if call.call_id in chat.announced:
                call.status = "failed"
                await self._complete(chat, call, sink)
        chat.calls.clear()

    async def _report_usage(self, chat: _Chat, result: Mapping[str, Any], sink: TurnSink) -> None:
        """The turn's usage, with ACP's context window and cost in `_meta`.

        AHP's `UsageInfo` has token counts and a model, and `_meta` for
        "additional provider-specific metadata"; no well-known key exists for
        a context window or a cost. So ACP's own `UsageUpdate` goes there under
        one key, `acpUsage` (`used`, `size`, `cost: {amount, currency}`),
        verbatim, where a client that knows ACP can read it and none can
        mistake it for something the spec defines.
        """
        usage = _mapping(result.get("usage"))
        input_tokens = usage.get("inputTokens")
        if not isinstance(input_tokens, int):
            # No per-turn usage: the agent's context size is the best gauge.
            input_tokens = chat.context_used
        output_tokens = usage.get("outputTokens")
        cached = usage.get("cachedReadTokens")
        acp_usage: dict[str, Any] = {}
        for key, value in (
            ("used", chat.context_used),
            ("size", chat.context_size),
            ("cost", chat.cost),
        ):
            if value is not None:
                acp_usage[key] = value
        if input_tokens is None and not isinstance(output_tokens, int) and not acp_usage:
            return
        await sink.usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            cache_read_tokens=cached if isinstance(cached, int) else None,
            model=chat.applied_model,
            meta={"acpUsage": acp_usage} if acp_usage else None,
        )


def _current_model(chat: _Chat) -> str | None:
    if chat.applied_model:
        return chat.applied_model
    option = chat.options.model_option
    return option.current if option is not None and isinstance(option.current, str) else None


class AcpProvider:
    """One host, one provider: an ACP agent command, rooted at the served folders."""

    #: `DeclaresCompletionTriggers`: the agent's slash commands open on `/`.
    completion_trigger_characters: Final = (slash.TRIGGER,)

    def __init__(
        self,
        roots: Path | Roots,
        spec: AgentSpec,
        *,
        display_name: str = "ACP agent",
        description: str = DEFAULT_DESCRIPTION,
        models: Sequence[ModelInfo] = (),
        provider_id: str = PROVIDER_ID,
        connect: Connector | None = None,
        catalogue_file: Path | None = None,
    ) -> None:
        if not is_valid_provider_id(provider_id):
            raise ValueError(f"invalid provider id: {provider_id!r}")
        if not spec.command:
            raise ValueError("an agent command is required")
        self.provider_id = provider_id
        self.roots = as_roots(roots)
        self.spec = spec
        self._display_name = display_name
        self._description = description
        self._configured_models = tuple(models)
        self._connect: Connector = connect or AcpConnection.spawn
        #: What the agent said about itself last time; see `catalogue`.
        self.catalogue = Catalogue(catalogue_file)
        self._info = self._describe()
        self._agent_updates: AgentInfoChanged | None = None
        self._sessions: weakref.WeakSet[AcpSession] = weakref.WeakSet()

    @property
    def agent(self) -> AgentInfo:
        return self._info

    def _describe(self) -> AgentInfo:
        """`AgentInfo` from the config and what the agent last reported.

        `multipleChats`: every ACP agent can hold several sessions, which is
        what a chat is here. `fork` only when the agent's last `initialize`
        said it can (`sessionCapabilities.fork`); `sideChat` never, since ACP
        has no way to give a session context outside its own conversation.
        """
        multiple: dict[str, Any] = {}
        if self.catalogue.session_capability("fork"):
            multiple["fork"] = True
        return AgentInfo(
            provider=self.provider_id,
            display_name=self._display_name,
            description=self._description,
            models=self.current_models(),
            capabilities={"multipleChats": multiple},
        )

    async def attach_agent_updates(self, changed: AgentInfoChanged) -> None:
        """`UpdatesAgentInfo`: keep the host's notifier for `_agent_changed`."""
        self._agent_updates = changed

    async def _agent_changed(self) -> None:
        """Republish `AgentInfo` if what the agent reported changed it.

        Called whenever it might have: a session's `session/new` (models), a
        `usage_update` (a model's context window), an `initialize` (whether
        the agent forks). The host compares and publishes `root/agentsChanged`
        only on a real difference.
        """
        described = self._describe()
        if described.to_wire() == self._info.to_wire():
            return
        self._info = described
        notify = self._agent_updates
        if notify is not None:
            try:
                await notify()
            except Exception:
                log.exception("republishing %s's agent info failed", self.provider_id)

    @property
    def default_model(self) -> str | None:
        """The config file's first model. With the agent's own list, none:
        a new session keeps the model the agent starts it with."""
        return self._configured_models[0].id if self._configured_models else None

    def current_models(self) -> tuple[ModelInfo, ...]:
        """The model picker, as things stand.

        The config file's `[[models]]` when it has any: they are a choice the
        operator made, and carry what the agent does not report (vision). The
        agent's own list otherwise -- its `model` config option, from the
        catalogue -- so an agent that can switch models gets a picker without
        a hand-written list. Either way a model's context window, when the
        config does not give one, is the size the agent last reported for it.
        """
        windows = self.catalogue.context_windows
        if self._configured_models:
            return tuple(
                dataclasses.replace(
                    model,
                    max_context_window=windows[model.id],
                    max_prompt_tokens=model.max_prompt_tokens or windows[model.id],
                )
                if model.max_context_window is None and model.id in windows
                else model
                for model in self._configured_models
            )
        return tuple(
            ModelInfo(
                id=model_id,
                name=name,
                max_context_window=windows.get(model_id),
                max_prompt_tokens=windows.get(model_id),
            )
            for model_id, name in self.catalogue.models()
        )

    def _session(self, context: AgentSessionContext, **kwargs: Any) -> AcpSession:
        session = AcpSession(
            context,
            roots=self.roots,
            spec=self.spec,
            connect=self._connect,
            default_model=self.default_model,
            catalogue=self.catalogue,
            on_agent_changed=self._agent_changed,
            **kwargs,
        )
        self._sessions.add(session)
        return session

    async def create_session(self, context: AgentSessionContext) -> AcpSession:
        session = self._session(context, config_properties=self._config_properties())
        fork = context.fork
        if fork is not None and fork.turns:
            source = next(
                (s for s in list(self._sessions) if s.context.session_uri == fork.session_uri),
                None,
            )
            await session.fork_from(source, fork)
        return session

    async def resume_session(self, context: AgentSessionContext) -> AcpSession:
        state = context.resume_state or {}
        session_id = state.get("acpSessionId")
        turns = state.get("turns")
        chats = state.get("chats")
        session = self._session(
            context,
            acp_session_id=session_id if isinstance(session_id, str) else None,
            directory=Path(cwd) if isinstance(cwd := state.get("cwd"), str) else None,
            # Absent in a state saved before turns were counted: not known.
            turns=turns if isinstance(turns, int) and not isinstance(turns, bool) else None,
            chats=chats if isinstance(chats, Mapping) else None,
        )
        if isinstance(model := state.get("model"), str):
            session.model = model
        return session

    async def resume_state_of(self, session: Any) -> Mapping[str, Any] | None:
        return session.resume_state() if isinstance(session, AcpSession) else None

    # -- ConfiguresSessions ------------------------------------------------------

    def _config_properties(self) -> dict[str, dict[str, Any]]:
        """The agent's options as session config, as it reported them last.

        The model option is left to the model picker when there is one.
        """
        return self.catalogue.options.properties(
            with_model=not self._info.models, pinned=self.spec.config_options
        )

    async def resolve_config(self, request: ConfigRequest) -> ConfigResolution:
        """The agent's options and modes, with its starting values as defaults.

        Empty until the agent has opened a session once (on this host, or on
        an earlier run with a catalogue file): options are the agent's to
        report, and asking would mean starting it, and a stray session in its
        history, just to fill in a form.
        """
        properties = self._config_properties()
        values = {key: prop["default"] for key, prop in properties.items() if "default" in prop}
        for key, value in request.values.items():
            prop = properties.get(key)
            if prop is not None and prop.get("sessionMutable") and _accepts(prop, value):
                values[key] = value
        return ConfigResolution(properties=properties, values=values)

    async def complete_config(self, request: ConfigRequest) -> Sequence[ConfigValue]:
        """No property is `enumDynamic`: every option lists its values."""
        return ()

    # -- Completes ---------------------------------------------------------------

    async def complete(self, request: CompletionRequest) -> Sequence[CompletionItem]:
        """Slash commands: the chat's own, its session's, or what the agent offered last.

        A chat's agent session starts on its first turn, so until then the
        commands are the ones the agent reported most recently -- the same
        agent's, so the same names, short of a project's own.
        """
        if request.kind not in ("", "userMessage"):
            return ()
        commands: tuple[slash.Command, ...] | None = None
        for session in list(self._sessions):
            if session.hosts(request.chat):
                commands = session.commands_for(request.chat)
                break
        if commands is None:
            commands = self.catalogue.commands
        return slash.complete(commands, request)


def _accepts(prop: Mapping[str, Any], value: Any) -> bool:
    """Whether *value* fits a property this adapter built (a select or a boolean)."""
    if prop.get("type") == "boolean":
        return isinstance(value, bool)
    allowed = prop.get("enum")
    return isinstance(value, str) and (not isinstance(allowed, list) or value in allowed)


def _reports_options(result: Mapping[str, Any]) -> bool:
    """Whether an answer states the agent's options (each report is complete)."""
    return isinstance(result.get("configOptions"), list) or isinstance(result.get("modes"), Mapping)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text_block(content: Any) -> str:
    block = _mapping(content)
    return str(block.get("text", "")) if block.get("type") == "text" else ""


def _consume(task: asyncio.Task[Any]) -> None:
    """Retrieve a background prompt's outcome so asyncio does not log it as lost."""
    if not task.cancelled():
        task.exception()


def _version() -> str:
    from ahp_host_acp import __version__

    return __version__
