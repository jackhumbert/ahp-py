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

Client tools: the tools a session's clients publish (and the host's own
per-turn tools) reach the agent as one MCP server, `ahp_client`, which is the
stdio relay of :mod:`ahp_host_acp.client_tools`. Each call goes through
`TurnSink.run_client_tool`, so it runs in the client; its row is the one that
call publishes, and the agent's own row for it is dropped. Calls to these tools
are let through without asking (:func:`ahp_host_acp.permissions.relayed_answer`).
ACP has no request to change a live session's `mcpServers`, so the server is
fixed when the session is opened and only its tool *list* is dynamic: the relay
answers every `tools/list` from the current state and sends
`notifications/tools/list_changed` when it moves. An agent that caches its
tools and ignores `list_changed` keeps the set it first listed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import sys
import uuid
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from ahp_acp import (
    AcpConnection,
    AcpError,
    AgentExitedError,
    AgentMessageChunk,
    AgentThoughtChunk,
    AvailableCommandsUpdate,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    MethodNotFoundError,
    PlanUpdate,
    SessionInfoUpdate,
    UsageUpdate,
    parse_update,
)
from ahp_acp import ToolCall as AcpToolCall
from ahp_acp import ToolCallUpdate as AcpToolCallUpdate
from ahp_acp.connection import NotificationHandler, RequestHandler
from ahp_host import AhpError
from ahp_host.provider.base import (
    AgentInfo,
    AgentInfoChanged,
    AgentSessionContext,
    ChatContext,
    ClientToolCall,
    CompletionItem,
    CompletionRequest,
    ConfigRequest,
    ConfigResolution,
    ConfigValue,
    ForkedFrom,
    ModelInfo,
    ToolConfirmation,
    ToolConfirmationOutcome,
    TurnSink,
    UserMessage,
)
from ahp_host.provider.policy import ToolPolicy
from ahp_host.provider.spawn import child_env

from ahp_host_acp import client_methods, client_tools, permissions, prompts
from ahp_host_acp import commands as slash
from ahp_host_acp import plan as plans
from ahp_host_acp.catalogue import Catalogue
from ahp_host_acp.changes import Diff, SessionEdits, diffs_of
from ahp_host_acp.client_methods import ExtHandler
from ahp_host_acp.client_tools import RelayTool
from ahp_host_acp.mcp import McpServer
from ahp_host_acp.options import AgentOptions, coerce, same
from ahp_host_acp.paths import directory_of
from ahp_host_acp.roots import Roots, as_roots
from ahp_host_acp.side_chat import SideChatMode, SideChatOrigin, SideChatSeed, render_preamble
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
        /,
        *,
        cwd: Path,
        env: Mapping[str, str] | None,
        inherit_env: bool = True,
        on_notification: NotificationHandler,
        on_request: RequestHandler,
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
    #: A program and arguments the agent command is run behind, e.g. `docker run
    #: -i --rm -v {cwd}:{cwd} -w {cwd} img`: see `launch_argv`.
    launcher: tuple[str, ...] = ()
    #: Host variables (names or globs) the agent may inherit; `None` keeps the
    #: whole environment. See `ahp_host.provider.spawn.child_env`.
    env_allow: tuple[str, ...] | None = None
    #: The relay's command when it runs inside the container (the relay
    #: installed in the image); not resolved on the host. `None` runs this
    #: Python's `-m ahp_host_acp.relay`.
    relay_command: tuple[str, ...] | None = None
    #: The address the tool bridge listens on, one the container can reach;
    #: `None` is loopback.
    relay_host: str | None = None
    #: The host's tool policy, advisory for this agent: see `permissions`.
    policy: ToolPolicy | None = None
    #: Offer the agent `fs/*` and `terminal/*`, served by the host under the
    #: roots jail and `policy`: see :mod:`ahp_host_acp.client_methods`.
    client_methods: bool = False
    #: Tell the agent why a call was declined, as a text block on the chat's
    #: next prompt (ACP's outcome has no field for it).
    decline_feedback: bool = True
    #: Answers to the agent's extension requests (methods starting with `_`, such
    #: as `_vendor/workspace`), keyed by method name: see
    #: :class:`ahp_host_acp.client_methods.ExtRequestContext`. A `_` method with no
    #: handler is refused as not found. Python only, not a config-file key.
    ext_handlers: Mapping[str, ExtHandler] = field(default_factory=dict)
    #: Offer side chats (`multipleChats.sideChat`): see :mod:`ahp_host_acp.side_chat`.
    #: `None` (the default) advertises nothing and refuses a side chat.
    side_chat: SideChatMode | None = None


def launch_argv(spec: AgentSpec, cwd: Path) -> tuple[str, ...]:
    """What to execute: the agent command, behind the launcher if there is one.

    In the launcher `{cwd}`, `{uid}` and `{gid}` are replaced (plain
    `str.replace`: other braces stay). The agent command follows unchanged; it
    is the launcher's (a container's), so only the launcher's program is looked
    up on the host's PATH.
    """
    if not spec.launcher:
        return spec.command
    words = []
    for word in spec.launcher:
        word = word.replace("{cwd}", str(cwd))
        if sys.platform != "win32":
            word = word.replace("{uid}", str(os.getuid())).replace("{gid}", str(os.getgid()))
        words.append(word)
    return (*words, *spec.command)


class _Chat:
    """One AHP chat's own ACP session, in its AHP session's agent process."""

    def __init__(
        self,
        uri: str,
        *,
        acp_session_id: str | None = None,
        model: str | None = None,
        turns: int | None = 0,
        seed: SideChatSeed | None = None,
    ) -> None:
        self.uri = uri
        #: A side chat's seed, until the prompt carrying it was delivered.
        self.seed = seed
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
        #: The host's own tools for the turn running now (`StructuredOutput`),
        #: offered through the relay beside the session's client tools.
        self.host_tools: tuple[RelayTool, ...] = ()
        #: ACP call ids that are relayed tool calls: `run_client_tool` shows
        #: those, so the agent's own row for one would be a duplicate.
        self.relayed: set[str] = set()
        #: The last diffs each running call showed: its final update may
        #: replace them with a plain result.
        self.diffs: dict[str, list[Diff]] = {}
        self.context_used: int | None = None
        self.context_size: int | None = None
        self.cost: Mapping[str, Any] | None = None
        self.inflight: asyncio.Task[Any] | None = None
        #: Permission questions put to the user and not yet answered; a cancel
        #: settles each as ACP's `cancelled` outcome (see `AcpSession._cancel`).
        self.asking: set[asyncio.Task[ToolConfirmationOutcome]] = set()
        #: The agent's slash commands, once this session reports them.
        self.commands: tuple[slash.Command, ...] | None = None
        self.plan: tuple[plans.Entry, ...] | None = None
        #: How many AHP turns this conversation has been given, so a fork can
        #: tell whether ACP's whole-session fork copies exactly the turns the
        #: forked chat shows. ``None``: not known (a lost or replaced agent
        #: session, a state saved before this was kept).
        self.turns = turns
        #: Why calls were declined since the last prompt (`permissions.decline_line`),
        #: sent as one text block on the next one: ACP's outcome has no reason field.
        self.declines: list[str] = []

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
            seed=SideChatSeed.restored(state.get("sideSeed")),
        )

    def state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if self.acp_session_id:
            state["acpSessionId"] = self.acp_session_id
        if self.model:
            state["model"] = self.model
        if self.turns is not None:
            state["turns"] = self.turns
        if self.seed is not None:
            state["sideSeed"] = self.seed.state()
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
        creator = (
            [{"clientId": context.active_client_id, "tools": list(context.client_tools)}]
            if context.active_client_id is not None
            else []
        )
        self._client_tools = client_tools.offered(creator)
        self._methods = client_methods.ClientMethods(
            roots=self._roots,
            policy=spec.policy,
            env_allow=spec.env_allow,
            cwd=lambda: self.directory or self._roots.primary,
        )
        self._bridge = client_tools.ToolBridge(
            self._relay_tools,
            self._relay_call,
            host=spec.relay_host,
            relay_command=spec.relay_command,
        )

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
                await self._drop_connection(self._conn)
            cwd = self.working_directory()
            self.directory = cwd
            await self._bridge.start()
            spec = self._spec
            env = dict(spec.env) if spec.env_allow is None else child_env(spec.env_allow, spec.env)
            conn = await self._connect(
                launch_argv(spec, cwd),
                cwd=cwd,
                env=env,
                inherit_env=spec.env_allow is None,
                on_notification=self._on_notification,
                on_request=self._on_request,
            )
            try:
                init = await conn.request(
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        # Client filesystem and terminal only when the spec serves
                        # them (`client_methods`, behind the jail and the policy);
                        # otherwise the agent works with its own tools.
                        "clientCapabilities": {
                            "fs": {
                                "readTextFile": spec.client_methods,
                                "writeTextFile": spec.client_methods,
                            },
                            "terminal": spec.client_methods,
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

    def _base(self, chat: _Chat | None) -> dict[str, Any]:
        """The parameters every session request shares.

        The relay is last, after the configured servers. A request with no
        chat (a fork for another session) has none: that session's own
        process gives its chat one when it reopens the fork.
        """
        servers = self._mcp_servers()
        if chat is not None:
            relay = self._bridge.server(chat.uri).to_acp(self._capabilities)
            if relay is not None:
                servers.append(relay)
        return {"cwd": str(self.directory), "mcpServers": servers}

    async def _open(self, conn: AcpConnection, chat: _Chat) -> None:
        """Open *chat*'s session in this process: reattach it, or start one."""
        base = self._base(chat)
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
            if chat.seed is not None and chat.seed.meta:
                base["_meta"] = dict(chat.seed.meta)
            result = _mapping(await conn.request("session/new", base))
            session_id = result.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise AcpError(message="the agent's session/new returned no sessionId")
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

    async def _drop_connection(self, conn: AcpConnection | None) -> None:
        """Close *conn*, the agent process a caller used, if it is still the current one.

        Another chat may have started the next process since *conn* died; a
        late clean-up of the old one must not stop that.
        """
        if conn is None or conn is not self._conn:
            return
        self._conn = None
        await self._methods.aclose()
        if conn is not None:
            try:
                await conn.aclose()
            except Exception:  # a dying agent must not fail session disposal
                log.exception("closing the ACP agent failed")

    async def aclose(self) -> None:
        await self._drop_connection(self._conn)
        await self._methods.aclose()
        await self._bridge.aclose()

    # -- client tools ------------------------------------------------------------

    def _relay_tools(self, key: str) -> tuple[RelayTool, ...]:
        """The tools the relay for chat *key* offers now."""
        chat = self._chats.get(key)
        return client_tools.merged(chat.host_tools if chat else (), self._client_tools)

    async def _relay_call(
        self, key: str, tool: RelayTool, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """One relayed call: the client (or the host) runs it, in the chat's turn."""
        chat = self._chats.get(key)
        sink = chat.sink if chat else None
        if sink is None:
            return client_tools.error("No turn is running to ask the client in.")
        try:
            result = await sink.run_client_tool(
                ClientToolCall(
                    call_id=f"relay-{uuid.uuid4().hex}",
                    name=tool.published,
                    client_id=tool.client_id,
                    tool_input=arguments,
                    display_name=tool.title,
                )
            )
        except LookupError as exc:
            return client_tools.error(str(exc))
        return client_tools.mcp_result(result)

    async def active_clients_changed(self, clients: Sequence[Mapping[str, Any]]) -> None:
        """`FollowsActiveClients`: the tools on offer are the clients' as they now are."""
        offered = client_tools.offered(clients)
        if offered != self._client_tools:
            self._client_tools = offered
            self._bridge.tools_changed()

    def _live(self, chat: _Chat) -> AcpConnection | None:
        """The connection *chat*'s session is open on, if it is open now."""
        conn = self._conn
        if conn is None or conn.closed or chat.acp_session_id is None:
            return None
        return conn if chat.generation == self._generation else None

    async def _cancel(self, chat: _Chat) -> None:
        # ACP: on cancel the client answers every pending permission request
        # with `cancelled`. Settle them first, so the agent sees that before it
        # winds the turn down, and the user's open question is withdrawn.
        for asking in list(chat.asking):
            asking.cancel()
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
        saved = self._saved_chats.pop(uri, None)
        side = context.origin_kind == "sideChat"
        if side and saved is None and self._spec.side_chat is None:
            # Not advertised unless opted in: ACP has no way to give a session
            # context that is not part of its own conversation.
            raise AhpError(PERMISSION_DENIED, "this agent cannot open side chats")
        if saved is not None:
            self._chats[uri] = _Chat.restored(uri, saved, self._default.model)
            return
        chat = _Chat(uri, model=self._default.model)
        if side:
            chat.seed = await self._side_chat_seed(context)
        elif context.fork is not None:
            await self._fork(chat, context.fork)
        self._chats[uri] = chat

    async def _side_chat_seed(self, context: ChatContext) -> SideChatSeed | None:
        origin = context.origin or {}
        source = context.side_chat
        source_uri = source.chat_uri if source is not None else origin.get("chat")
        source_uri = source_uri if isinstance(source_uri, str) else None
        selection = origin.get("selection")
        known = self._chats.get(source_uri) if source_uri is not None else None
        turn_id = source.turn_id if source is not None else origin.get("turnId")
        seed = await self.side_chat_seed(
            SideChatOrigin(
                chat_uri=source_uri,
                acp_session_id=known.acp_session_id if known is not None else None,
                turn_id=turn_id if isinstance(turn_id, str) else None,
                selection=selection if isinstance(selection, Mapping) else None,
                turns=tuple(source.turns) if source is not None else (),
            )
        )
        return seed if seed is not None and (seed.meta or seed.preamble) else None

    async def side_chat_seed(self, origin: SideChatOrigin) -> SideChatSeed | None:
        """How to seed a side chat's ACP session; override to do it your way.

        Called once, as the side chat opens (`AgentSpec.side_chat` is set). The
        default is the built-in preamble (:func:`~ahp_host_acp.side_chat.render_preamble`)
        when the mode asks for it. Return ``None`` to seed nothing.
        """
        mode = self._spec.side_chat
        if mode is None or not mode.preamble:
            return None
        return SideChatSeed(preamble=render_preamble(origin, mode.max_chars))

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
            base = self._base(into)
            if cwd is not None:
                base["cwd"] = str(cwd)
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
            raise AcpError(message="the agent's session/fork returned no sessionId")
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
            if chat is not self._default and (chat.acp_session_id or chat.seed is not None):
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
        event = parse_update(params)
        chat = self._chat_of(event.session_id)
        if chat is None:
            return
        # Session state first: it is true whether or not a turn is running,
        # and a `session/load` replay brings it up to date too.
        if isinstance(event, UsageUpdate):
            await self._on_usage(chat, event)
            return
        if isinstance(event, ConfigOptionUpdate | CurrentModeUpdate):
            chat.options.note_update(event.raw)
            if isinstance(event, ConfigOptionUpdate):
                chat.verified = True
            await self._publish_config(chat)
            return
        if isinstance(event, AvailableCommandsUpdate):
            chat.commands = slash.parse_commands(event.commands)
            self._catalogue.remember_commands(event.commands)
            return
        if isinstance(event, SessionInfoUpdate):
            # The session is named after its default chat's conversation; a
            # replayed title is history, not a rename.
            if chat is self._default and not chat.quiet:
                await self._on_title(event)
            return
        if isinstance(event, PlanUpdate):
            await self._on_plan(chat, event)
            return
        sink = chat.sink
        if sink is None or chat.quiet:
            return
        if isinstance(event, AgentMessageChunk):
            text = _text_block(event.content)
            if text:
                await sink.text_delta(text)
        elif isinstance(event, AgentThoughtChunk):
            text = _text_block(event.content)
            if text:
                await sink.reasoning_delta(text)
        elif isinstance(event, AcpToolCall | AcpToolCallUpdate):
            await self._on_tool_call(chat, event.raw, sink)
        else:
            # A kind this provider has no use for, a user-message replay, or an
            # update missing a field its kind requires.
            log.debug("ignoring session update %s", getattr(event, "kind", type(event).__name__))

    async def _on_usage(self, chat: _Chat, update: UsageUpdate) -> None:
        """`usage_update`: tokens in context, the window's size, and the cost.

        `used` is the latest model call's prompt size; the last one seen in a
        turn is published as the turn's `inputTokens` (clients read that as the
        most recent model call), see :meth:`_report_usage`. `size` and `cost`
        ride on the turn's usage `_meta`, and `size` is remembered as the
        model's context window for the picker.
        """
        chat.context_used = update.used
        if update.size > 0:
            chat.context_size = update.size
            self._catalogue.remember_context_window(_current_model(chat), update.size)
            await self._on_agent_changed()
        cost = update.cost
        if cost is not None and isinstance(cost.get("amount"), int | float):
            chat.cost = dict(cost)

    async def _on_title(self, update: SessionInfoUpdate) -> None:
        """`session_info_update.title` renames the session.

        A null title ("set to null to clear") is not passed on: AHP has no
        untitled state to go back to, and the session keeps the name it has.
        """
        title = update.title
        publisher = self.context.publisher
        if title is None or not title.strip() or title == self._title:
            return
        self._title = title
        if publisher is not None:
            await publisher.title_changed(title)

    async def _on_plan(self, chat: _Chat, update: PlanUpdate) -> None:
        """A plan update: one finished "Update plan" row. See :mod:`ahp_host_acp.plan`."""
        entries = plans.parse_plan(update.entries)
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
            {"entries": update.entries},
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

    def _is_relayed(self, chat: _Chat, update: Mapping[str, Any]) -> bool:
        """Whether the agent's tool call is one of the relay's tools.

        The title, the kind and the raw input all have to fit (see
        `client_tools.looks_relayed`): only then is the call let through
        without asking, so a disguised local call is asked about as usual.
        """
        return client_tools.looks_relayed(update, self._relay_tools(chat.uri))

    async def _on_tool_call(self, chat: _Chat, update: Mapping[str, Any], sink: TurnSink) -> None:
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return
        if call_id in chat.relayed:
            return
        if self._is_relayed(chat, update):
            chat.relayed.add(call_id)  # `run_client_tool` publishes this call's row
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
        notice = [{"type": "text", "text": call.notice}] if call.notice else []
        await sink.tool_call_completed(
            call.call_id,
            {"content": [*notice, *text_of(call.content, shown=shown), *edits]},
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
        if self._spec.client_methods and method in client_methods.METHODS:
            chat = self._chat_of(_mapping(params).get("sessionId"))
            return await self._methods.handle(
                method, _mapping(params), chat.sink if chat is not None else None
            )
        if method.startswith("_") and (handler := self._spec.ext_handlers.get(method)) is not None:
            return await self._methods.handle_ext(handler, method, _mapping(params))
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
        # Judged on the call as merged so far, not on the request alone: an
        # agent may ask with only the id of a call it announced in full, and a
        # call with no kind gets the one its input shows (`ToolCall.merge`).
        call = chat.calls.setdefault(call_id, ToolCall(call_id))
        call.merge(update)
        judged = call.as_update()
        ask_anyway = False
        if self._spec.policy is not None:
            decision = self._spec.policy.decide(
                permissions.tool_request(
                    judged,
                    cwd=self.directory or self._roots.primary,
                    roots=tuple(self._roots.paths),
                )
            )
            if decision.action == "deny":
                reason = decision.reason or "denied by policy"
                log.info("tool call %s denied by policy: %s", call_id, reason)
                # The user sees why: under the call's name now, and in its
                # result when it ends (the agent's failure, or the turn's end).
                call.notice = reason
                if self._spec.decline_feedback:
                    chat.declines.append(
                        permissions.decline_line(call.title.strip() or call.name, reason)
                    )
                await self._announce(chat, call, sink)
                await sink.tool_call_delta(call_id, invocation_message=reason)
                return permissions.rejected_answer(offered)
            ask_anyway = decision.action == "ask"
        if (
            not ask_anyway
            and self._is_relayed(chat, judged)
            and (answer := permissions.relayed_answer(offered))
        ):
            # A client's (or the host's) own tool: its owner decides, and
            # `run_client_tool` shows the call. See `permissions.relayed_answer`.
            if call_id not in chat.announced:
                chat.calls.pop(call_id, None)
            chat.relayed.add(call_id)
            return answer
        self._note_diffs(chat, call)
        await self._announce(chat, call, sink)
        asking = asyncio.ensure_future(
            sink.confirm_tool_call(
                ToolConfirmation(
                    call_id=call_id,
                    name=call.name,
                    display_name=call.display_name,
                    invocation_message=call.approval_line(),
                    tool_input=call.raw_input,
                    confirmation_title=call.title or None,
                    options=permissions.confirmation_options(offered),
                    edits=self._edits.preview(diffs_of(call.content), self.directory),
                    read_only=permissions.read_only_hint(call.kind),
                    # The kind is the agent's word, not the host's: see the field.
                    read_only_from_agent=True,
                    locations=tuple(
                        loc["path"] for loc in call.locations if isinstance(loc.get("path"), str)
                    ),
                    policy_asked=ask_anyway,
                )
            )
        )
        chat.asking.add(asking)
        try:
            outcome = await asking
        except asyncio.CancelledError:
            return permissions.CANCELLED  # cancelled, or the turn ended while the prompt was open
        finally:
            chat.asking.discard(asking)
        if self._spec.decline_feedback and not outcome.approved:
            declined_why = outcome.reason_message
            suggestion = getattr(outcome.user_suggestion, "text", None)
            if isinstance(suggestion, str) and suggestion:
                declined_why = (
                    f"{declined_why} (suggested instead: {suggestion})"
                    if declined_why
                    else f"(suggested instead: {suggestion})"
                )
            if declined_why:
                chat.declines.append(
                    permissions.decline_line(call.title.strip() or call.name, declined_why)
                )
        return permissions.answer(offered, outcome)

    # -- turns ---------------------------------------------------------------

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        chat = self._chat_for(message.chat_uri)
        async with chat.lock:
            await self._settle(chat)
            if message.model is not None:
                chat.model = message.model.id
            used: AcpConnection | None = None
            try:
                used = await self._ensure_process()
                conn = used = await self._ensure(chat)
                await self._sync_config(conn, chat)
                await self._apply_model(conn, chat)
            except PermissionError as error:
                await sink.turn_failed(str(error), error_type="agent.workingDirectory")
                return
            except (AcpError, OSError) as error:
                if isinstance(error, AgentExitedError):
                    await self._drop_connection(used)
                await sink.turn_failed(f"Could not start the agent: {error}", "acp.start")
                return
            chat.sink = sink
            chat.calls.clear()
            chat.announced.clear()
            chat.diffs.clear()
            chat.relayed.clear()
            # A follow-up inside one AHP turn is not a turn: the fork rule
            # compares turn counts with what the chat shows.
            if chat.turns is not None and not message.continues_turn:
                chat.turns += 1
            tools = client_tools.host_relay_tools(message.host_tools)
            if tools != chat.host_tools:
                chat.host_tools = tools
                self._bridge.tools_changed(chat.uri)
            try:
                await self._run_turn(conn, chat, message, sink)
            finally:
                chat.sink = None
                if chat.host_tools:
                    chat.host_tools = ()
                    self._bridge.tools_changed(chat.uri)

    def offers_host_tools(self) -> bool:
        """`ReturnsStructuredOutput`: every session has the relay (ACP requires stdio MCP)."""
        return True

    async def _settle(self, chat: _Chat) -> None:
        inflight, chat.inflight = chat.inflight, None
        if inflight is not None and not inflight.done():
            await asyncio.wait({inflight}, timeout=SETTLE_TIMEOUT)

    async def _prompt(
        self,
        conn: AcpConnection,
        chat: _Chat,
        blocks: Sequence[Mapping[str, Any]],
        *,
        meta: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """One `session/prompt`, shielded: a stopped turn lets it finish in the background."""
        params: dict[str, Any] = {"sessionId": chat.acp_session_id, "prompt": list(blocks)}
        if meta:
            params["_meta"] = dict(meta)
        task = asyncio.create_task(conn.request("session/prompt", params))
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
        # The reasons go out with this prompt and are dropped only once it was delivered:
        # an agent that dies on the prompt never read them, so the retry carries them again.
        # Declines made during this turn are appended after them, for the next prompt.
        side, chat.seed = chat.seed, None
        if side is not None and side.preamble:
            blocks.insert(0, prompts.text(side.preamble))
        sent_declines = len(chat.declines)
        if sent_declines:
            # Last, so the agent's first text block stays the user's message.
            blocks.append({"type": "text", "text": "\n".join(chat.declines)})
        try:
            result = await self._prompt(conn, chat, blocks, meta=side.meta if side else None)
        except asyncio.CancelledError:
            # The prompt was in flight (the agent has it); a cancel does not resend it.
            del chat.declines[:sent_declines]
            await self._cancel(chat)
            raise
        except AgentExitedError as error:
            chat.seed = side or chat.seed  # the agent may never have read it
            await self._close_open_calls(chat, sink)
            await self._drop_connection(conn)
            chat.turns = None  # whether the agent kept this turn is anyone's guess
            await sink.turn_failed(str(error), error_type="acp.agentExited")
            return
        except AcpError as error:
            await self._close_open_calls(chat, sink)
            await sink.turn_failed(str(error), error_type=f"acp.{error.code}")
            return
        del chat.declines[:sent_declines]
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
        mistake it for something the spec defines. When the turn has no
        `inputTokens` and the context size stands in for it, `acpUsage` says so
        (`inputGauge: true`): the figure is how full the context is, not what the
        turn consumed, and a reader that sums usage (a budget) must not count it.

        `inputTokens` is the most recent model call, which clients read as the
        context-window meter: the last `usage_update.used` of the turn when one
        arrived. ACP's turn usage sums every round, so that sum goes to
        `_meta.turnTokenTotals` (`[{model, inputTokens, cachedTokens,
        outputTokens}]`) and is `inputTokens` only for an agent that sends no
        `usage_update`.

        The cache reads beside `inputTokens` are the same call's: ACP reports
        them only for the turn, so beside the latest call's input there are
        none, and the turn's go with the turn's input, capped at it: a reader
        takes the pair as a share of that input. `turnTokenTotals` keeps the
        turn's sums as the agent reported them, uncapped, since an agent may
        count cache reads apart from its input (the reference agent does).
        """
        usage = _mapping(result.get("usage"))
        turn_input = usage.get("inputTokens")
        input_tokens = turn_input
        # With no per-turn usage the agent's context size is a gauge, not a count.
        gauge = chat.context_used is not None and not isinstance(turn_input, int)
        output_tokens = usage.get("outputTokens")
        turn_cached = usage.get("cachedReadTokens")
        cached = None
        if isinstance(turn_cached, int) and isinstance(turn_input, int):
            cached = min(turn_cached, turn_input)
        if chat.context_used is not None:
            input_tokens = chat.context_used
            cached = None
        acp_usage: dict[str, Any] = {}
        for key, value in (
            ("used", chat.context_used),
            ("size", chat.context_size),
            ("cost", chat.cost),
        ):
            if value is not None:
                acp_usage[key] = value
        if gauge:
            acp_usage["inputGauge"] = True
        if input_tokens is None and not isinstance(output_tokens, int) and not acp_usage:
            return
        meta: dict[str, Any] = {}
        if acp_usage:
            meta["acpUsage"] = acp_usage
        if isinstance(turn_input, int) or isinstance(output_tokens, int):
            meta["turnTokenTotals"] = [
                {
                    "model": _current_model(chat) or "",
                    "inputTokens": turn_input if isinstance(turn_input, int) else 0,
                    "cachedTokens": turn_cached if isinstance(turn_cached, int) else 0,
                    "outputTokens": output_tokens if isinstance(output_tokens, int) else 0,
                }
            ]
        await sink.usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            cache_read_tokens=cached if isinstance(cached, int) else None,
            model=chat.applied_model,
            meta=meta or None,
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
    #: The session type to create; a subclass of `AcpSession` here can override
    #: hooks such as `AcpSession.side_chat_seed`.
    session_class: type[AcpSession] = AcpSession

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
        said it can (`sessionCapabilities.fork`); `sideChat` only when
        `AgentSpec.side_chat` opts in, since ACP has no way to give a session
        context outside its own conversation (see :mod:`ahp_host_acp.side_chat`).
        """
        multiple: dict[str, Any] = {}
        if self.catalogue.session_capability("fork"):
            multiple["fork"] = True
        if self.spec.side_chat is not None:
            multiple["sideChat"] = True
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
        session = self.session_class(
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
