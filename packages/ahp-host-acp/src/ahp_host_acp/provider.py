"""The adapter: one ACP agent process per AHP session.

Each session spawns the configured agent command (`openclaw acp`, or any
other ACP agent), opens one ACP session in it, and translates:

- `session/prompt` <- a user turn; its streamed `session/update`s become the
  host's neutral `TurnSink` events (text, reasoning, tool calls, usage);
- `session/request_permission` -> the host's `confirm_tool_call`, which is
  where a client shows an approval prompt;
- a stop -> `session/cancel`.

Out of turn, through the session's `SessionPublisher`:

- the agent's config options and modes are the session's config, both ways
  (:mod:`ahp_host_acp.options`); the title it reports (`session_info_update`)
  is the session's title; the files its tool calls edit are the session's
  changeset (:mod:`ahp_host_acp.changes`);
- its slash commands are offered as completions (:mod:`ahp_host_acp.commands`),
  and its plan is shown as a row per update (:mod:`ahp_host_acp.plan`).

What this adapter cannot do is decide *which* calls need approval: the agent
asks, or it does not. See the README's Security section.

Models: an agent with a `model` session config option (opencode) is switched
with `session/set_config_option`; one that reports ACP session models (an API
ACP has since removed), with `session/set_model`. One that does neither
(OpenClaw's bridge) can be given a `model_command`, a prompt template such as
``/model {model} -s`` sent as its own turn whenever the picked model changes;
its reply is not shown.

Sessions are resumable: the ACP session id is the resume state, restored with
`session/resume` (or `session/load`) when the agent offers it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import uuid
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from ahp_host.provider.base import (
    AgentInfo,
    AgentSessionContext,
    CompletionItem,
    CompletionRequest,
    ConfigRequest,
    ConfigResolution,
    ConfigValue,
    ModelInfo,
    ToolConfirmation,
    TurnSink,
    UserMessage,
)

from ahp_host_acp import commands as slash
from ahp_host_acp import jsonrpc
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
        on_models_changed: Callable[[], None] | None = None,
    ) -> None:
        self.context = context
        self._roots = as_roots(roots)
        self._spec = spec
        self._connect = connect
        #: Set on resume: the folder the session was started in.
        self.directory = directory
        self.acp_session_id = acp_session_id
        #: The model the user wants, and the one the agent was last switched to.
        self.model: str | None = context.model or default_model
        self._applied_model: str | None = None
        self._native_models = False
        self._conn: AcpConnection | None = None
        self._capabilities: Mapping[str, Any] = {}
        self._sink: TurnSink | None = None
        #: While true, updates are dropped: a model switch's reply, or history
        #: an agent replays on `session/load`.
        self._quiet = False
        #: While a session is being opened its id is not known yet, and an
        #: agent may send updates right behind its `session/new` answer.
        self._opening = False
        self._calls: dict[str, ToolCall] = {}
        self._announced: set[str] = set()
        #: The last diffs each running call showed: its final update may
        #: replace them with a plain result.
        self._diffs: dict[str, list[Diff]] = {}
        self._context_used: int | None = None
        self._context_size: int | None = None
        self._cost: Mapping[str, Any] | None = None
        self._inflight: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()
        self._catalogue = catalogue if catalogue is not None else Catalogue()
        self._on_models_changed = on_models_changed or (lambda: None)

        # -- session config ----------------------------------------------------
        #: What the agent reported, kept across a restarted agent process (the
        #: options' shape is the agent's); `_verified` says whether the
        #: *current* process has reported its values yet.
        self._options = AgentOptions()
        self._verified = False
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
        #: What has been sent to the current agent process, per option.
        self._applied: dict[str, Any] = {}
        self._config_lock = asyncio.Lock()
        #: While non-zero, the agent's values are not republished: they are
        #: about to be overwritten with the session's own.
        self._hold = 0

        #: The agent's slash commands, once this session's process reports them.
        self.commands: tuple[slash.Command, ...] | None = None
        self._title: str | None = None
        self._plan: tuple[plans.Entry, ...] | None = None
        self._edits = SessionEdits(self._roots)

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

    async def _ensure(self) -> AcpConnection:
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
        self._hold += 1
        try:
            init = await conn.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    # No client filesystem or terminal: the agent works with its
                    # own tools, and every host-side read goes through the jail.
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "ahp-host-acp", "version": _version()},
                },
            )
            self._capabilities = _mapping(_mapping(init).get("agentCapabilities"))
            self._applied = {}
            self._verified = False
            await self._open_session(conn, cwd)
            await self._sync_config(conn)
        except BaseException:
            await conn.aclose()
            raise
        finally:
            self._hold -= 1
        self._conn = conn
        await self._publish_config()
        return conn

    def _mcp_servers(self) -> list[dict[str, Any]]:
        servers = (server.to_acp(self._capabilities) for server in self._spec.mcp_servers)
        return [server for server in servers if server is not None]

    async def _open_session(self, conn: AcpConnection, cwd: Path) -> None:
        base = {"cwd": str(cwd), "mcpServers": self._mcp_servers()}
        self._opening = True
        try:
            if self.acp_session_id is not None:
                if await self._reattach(conn, base):
                    return
                log.warning(
                    "could not reopen ACP session %s; starting a new one", self.acp_session_id
                )
            result = _mapping(await conn.request("session/new", base))
            session_id = result.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise AcpError("the agent's session/new returned no sessionId")
            self.acp_session_id = session_id
        finally:
            self._opening = False
        self._note_session(result)
        self._catalogue.remember_new_session(result)
        self._on_models_changed()
        self._applied_model = None

    async def _reattach(self, conn: AcpConnection, base: Mapping[str, Any]) -> bool:
        params = {**base, "sessionId": self.acp_session_id}
        session_caps = _mapping(self._capabilities.get("sessionCapabilities"))
        try:
            if "resume" in session_caps:
                result = await conn.request("session/resume", params)
            elif self._capabilities.get("loadSession"):
                # `session/load` replays the conversation as updates; the
                # client already shows it, so the replay is not published.
                self._quiet = True
                try:
                    result = await conn.request("session/load", params)
                finally:
                    self._quiet = False
            else:
                return False
        except AcpError as exc:
            log.warning("reopening ACP session %s failed: %s", self.acp_session_id, exc)
            return False
        self._note_session(_mapping(result))
        # The agent's own model may have survived, but not provably: switch again.
        self._applied_model = None
        return True

    def _note_session(self, result: Mapping[str, Any]) -> None:
        """What a session/new, resume or load answer says about configuration.

        Newer agents (opencode) offer the model as a session config option of
        category `model`; older ones report `models` for `session/set_model`.
        An answer that says neither (some agents' `session/resume`) keeps what
        was known about the options' shape, but not about their values: this
        agent process has not said what they are.
        """
        if "models" in result:
            self._native_models = bool(_mapping(result.get("models")).get("availableModels"))
        self._options.note(result)
        if _reports_options(result):
            self._verified = True

    @property
    def _model_option(self) -> str | None:
        """The id of the agent's model config option (`category: "model"`), if any."""
        option = self._options.model_option
        return option.id if option is not None else None

    async def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.aclose()
            except Exception:  # a dying agent must not fail session disposal
                log.exception("closing the ACP agent failed")

    async def aclose(self) -> None:
        await self._drop_connection()

    async def cancel(self, reason: str | None = None) -> None:
        conn = self._conn
        if conn is not None and self.acp_session_id is not None and not conn.closed:
            try:
                await conn.notify("session/cancel", {"sessionId": self.acp_session_id})
            except AcpError:
                log.debug("session/cancel did not reach the agent", exc_info=True)

    # -- session config --------------------------------------------------------

    def _wanted(self) -> dict[str, Any]:
        """What each option should be: the config file's, else the session's."""
        pinned = self._spec.config_options
        wanted = {key: value for key, value in self._config.items() if key not in pinned}
        wanted.update(pinned)
        return wanted

    async def _sync_config(self, conn: AcpConnection) -> None:
        """Set every option the agent does not already have as wanted.

        Each value is sent once per agent process: a refusal is logged and not
        retried every turn, and an option the agent later changes itself is
        not changed back. A refused or ignored client change is then undone
        in the session's state by :meth:`_publish_config`, from what the
        agent reports.
        """
        async with self._config_lock:
            self._hold += 1
            try:
                for key, value in self._wanted().items():
                    option = self._options.get(key)
                    value = coerce(option, value) if option is not None else value
                    if self._verified and option is not None and same(option.current, value):
                        self._applied[key] = value
                        continue
                    if key in self._applied and same(self._applied[key], value):
                        continue
                    self._applied[key] = value
                    method, params = self._options.request_for(key, value)
                    try:
                        result = await conn.request(
                            method, {"sessionId": self.acp_session_id, **params}
                        )
                    except AgentExitedError:
                        raise
                    except AcpError as exc:
                        log.warning("the agent refused %s=%s: %s", key, value, exc)
                        continue
                    answer = _mapping(result)
                    self._options.note(answer)
                    if _reports_options(answer):
                        self._verified = True
                    else:
                        self._options.assume(key, value)
            finally:
                self._hold -= 1
        await self._publish_config()

    async def _publish_config(self) -> None:
        """Tell clients the values the agent reports, where they differ from
        the session's. The agent is what is actually in force."""
        if self._hold or not self._verified:
            return
        changed: dict[str, Any] = {}
        for key in self._properties:
            reported = self._options.value_of(key)
            if reported is None:
                continue
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

        Applied straight away when the agent is running (ACP lets a mode change
        mid-turn); otherwise on its next start.
        """
        for key, value in values.items():
            if key in self._properties and key not in self._spec.config_options:
                self._config[key] = value
        conn = self._conn
        if conn is None or conn.closed or self.acp_session_id is None:
            return
        try:
            await self._sync_config(conn)
        except AcpError as exc:
            log.warning("could not reconfigure ACP session %s: %s", self.acp_session_id, exc)

    # -- agent -> client -------------------------------------------------------

    def _ours(self, session_id: Any) -> bool:
        if session_id == self.acp_session_id:
            return True
        # One agent process per session: while ours is being opened, an
        # update for a session this process just created can only be for it.
        return self._opening and isinstance(session_id, str)

    async def _on_notification(self, method: str, params: Any) -> None:
        if method != "session/update":
            log.debug("ignoring ACP notification %s", method)
            return
        params = _mapping(params)
        if not self._ours(params.get("sessionId")):
            return
        update = _mapping(params.get("update"))
        kind = update.get("sessionUpdate")
        # Session state first: it is true whether or not a turn is running,
        # and a `session/load` replay brings it up to date too.
        if kind == "usage_update":
            self._on_usage(update)
            return
        if kind in ("config_option_update", "current_mode_update"):
            self._options.note_update(update)
            if kind == "config_option_update":
                self._verified = True
            await self._publish_config()
            return
        if kind == "available_commands_update":
            raw = update.get("availableCommands")
            self.commands = slash.parse_commands(raw)
            self._catalogue.remember_commands(raw)
            return
        if kind == "session_info_update":
            if not self._quiet:  # a replayed title is history, not a rename
                await self._on_title(update)
            return
        if kind == "plan":
            await self._on_plan(update)
            return
        sink = self._sink
        if sink is None or self._quiet:
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
            await self._on_tool_call(update, sink)
        else:
            log.debug("ignoring session update %s", kind)

    def _on_usage(self, update: Mapping[str, Any]) -> None:
        """`usage_update`: tokens in context, the window's size, and the cost.

        `used` stands in for a turn's input tokens when the prompt's answer
        has none; `size` and `cost` ride on the turn's usage `_meta` (see
        :meth:`_report_usage`), and `size` is remembered as the model's
        context window for the picker.
        """
        used, size, cost = update.get("used"), update.get("size"), update.get("cost")
        if isinstance(used, int) and not isinstance(used, bool):
            self._context_used = used
        if isinstance(size, int) and not isinstance(size, bool) and size > 0:
            self._context_size = size
            self._catalogue.remember_context_window(self._current_model(), size)
            self._on_models_changed()
        if isinstance(cost, Mapping) and isinstance(cost.get("amount"), int | float):
            self._cost = dict(cost)

    def _current_model(self) -> str | None:
        if self._applied_model:
            return self._applied_model
        option = self._options.model_option
        return option.current if option is not None and isinstance(option.current, str) else None

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

    async def _on_plan(self, update: Mapping[str, Any]) -> None:
        """A plan update: one finished "Update plan" row. See :mod:`ahp_host_acp.plan`."""
        entries = plans.parse_plan(update.get("entries"))
        if entries == self._plan:
            return  # resent unchanged
        self._plan = entries
        sink = self._sink
        if sink is None or self._quiet:
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

    async def _on_tool_call(self, update: Mapping[str, Any], sink: TurnSink) -> None:
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return
        call = self._calls.setdefault(call_id, ToolCall(call_id))
        before = call.progress_line()
        call.merge(update)
        await self._announce(call, sink)
        if call.finished:
            await self._complete(call, sink)
            return
        if "content" in update:
            self._note_diffs(call)
        if call.progress_line() != before:
            await sink.tool_call_delta(call_id, invocation_message=call.progress_line())
        if "content" in update and call.content:
            await sink.tool_call_output(call_id, text_of(call.content))

    async def _announce(self, call: ToolCall, sink: TurnSink) -> None:
        if call.call_id in self._announced:
            return
        self._announced.add(call.call_id)
        await sink.tool_call_started(
            call.call_id, call.name, call.raw_input, display_name=call.display_name
        )
        await sink.tool_call_delta(call.call_id, invocation_message=call.progress_line())

    async def _complete(self, call: ToolCall, sink: TurnSink) -> None:
        if self._calls.pop(call.call_id, None) is None:
            return
        await sink.tool_call_completed(
            call.call_id,
            {"content": text_of(call.content)},
            success=call.status != "failed",
            past_tense_message=call.past_tense(),
        )
        diffs = diffs_of(call.content) or self._diffs.pop(call.call_id, [])
        self._diffs.pop(call.call_id, None)
        if not diffs:
            return
        if call.status != "completed":
            self._edits.abandoned(diffs, self.directory)
        elif self._edits.completed(diffs, self.directory):
            await self._publish_changes()

    def _note_diffs(self, call: ToolCall) -> None:
        """A running call shows diffs: remember them, and the files before it runs."""
        diffs = diffs_of(call.content)
        if diffs:
            self._diffs[call.call_id] = diffs
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
        """Put the agent's question to the user. Only ever grants *once*.

        An "always allow" option would widen what the agent may do without
        asking again, in the agent's own state where this host cannot see or
        undo it; approving here picks the one-time option whenever there is one.
        """
        cancelled = {"outcome": {"outcome": "cancelled"}}
        options = [o for o in params.get("options") or () if isinstance(o, Mapping)]
        sink = self._sink
        if sink is None or params.get("sessionId") != self.acp_session_id:
            return cancelled
        update = _mapping(params.get("toolCall"))
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return cancelled
        call = self._calls.setdefault(call_id, ToolCall(call_id))
        call.merge(update)
        self._note_diffs(call)
        await self._announce(call, sink)
        try:
            outcome = await sink.confirm_tool_call(
                ToolConfirmation(
                    call_id=call_id,
                    name=call.name,
                    display_name=call.display_name,
                    invocation_message=call.approval_line(),
                    tool_input=call.raw_input,
                    confirmation_title=call.title or None,
                )
            )
        except asyncio.CancelledError:
            return cancelled  # the turn ended while the prompt was open
        wanted = (
            ("allow_once", "allow_always") if outcome.approved else ("reject_once", "reject_always")
        )
        for kind in wanted:
            for option in options:
                if option.get("kind") == kind and isinstance(option.get("optionId"), str):
                    return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return cancelled

    # -- turns ---------------------------------------------------------------

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        async with self._lock:
            await self._settle()
            if message.model is not None:
                self.model = message.model.id
            try:
                conn = await self._ensure()
                await self._sync_config(conn)
                await self._apply_model(conn)
            except PermissionError as error:
                await sink.turn_failed(str(error), error_type="agent.workingDirectory")
                return
            except (AcpError, OSError) as error:
                await self._drop_connection()
                await sink.turn_failed(f"Could not start the agent: {error}", "acp.start")
                return
            self._sink = sink
            self._calls.clear()
            self._announced.clear()
            self._diffs.clear()
            try:
                await self._run_turn(conn, message, sink)
            finally:
                self._sink = None

    async def _settle(self) -> None:
        inflight, self._inflight = self._inflight, None
        if inflight is not None and not inflight.done():
            await asyncio.wait({inflight}, timeout=SETTLE_TIMEOUT)

    async def _prompt(self, conn: AcpConnection, text: str) -> Mapping[str, Any]:
        """One `session/prompt`, shielded: a stopped turn lets it finish in the background."""
        task = asyncio.create_task(
            conn.request(
                "session/prompt",
                {"sessionId": self.acp_session_id, "prompt": [{"type": "text", "text": text}]},
            )
        )
        self._inflight = task
        task.add_done_callback(_consume)
        return _mapping(await asyncio.shield(task))

    async def _apply_model(self, conn: AcpConnection) -> None:
        wanted = self.model
        if not wanted or wanted == self._applied_model:
            return
        if self._model_option is not None:
            result = await conn.request(
                "session/set_config_option",
                {"sessionId": self.acp_session_id, "configId": self._model_option, "value": wanted},
            )
            answer = _mapping(result)
            self._options.note(answer)
            if _reports_options(answer):
                self._verified = True
        elif self._native_models:
            await conn.request(
                "session/set_model", {"sessionId": self.acp_session_id, "modelId": wanted}
            )
        elif self._spec.model_command:
            self._quiet = True
            try:
                result = await self._prompt(conn, self._spec.model_command.format(model=wanted))
            finally:
                self._quiet = False
            log.info(
                "switched session %s to %s (%s)",
                self.acp_session_id,
                wanted,
                result.get("stopReason"),
            )
        self._applied_model = wanted

    async def _run_turn(self, conn: AcpConnection, message: UserMessage, sink: TurnSink) -> None:
        self._context_used = None
        try:
            result = await self._prompt(conn, message.text)
        except asyncio.CancelledError:
            await self.cancel("stopped")
            raise
        except AgentExitedError as error:
            await self._close_open_calls(sink)
            await self._drop_connection()
            await sink.turn_failed(str(error), error_type="acp.agentExited")
            return
        except AcpError as error:
            await self._close_open_calls(sink)
            await sink.turn_failed(str(error), error_type=f"acp.{error.code}")
            return
        await self._close_open_calls(sink)
        await self._report_usage(result, sink)
        stop = result.get("stopReason")
        if stop in ("end_turn", "cancelled", None):
            return
        reasons = {
            "max_tokens": "The agent hit its output token limit.",
            "max_turn_requests": "The agent hit its limit of model requests for one turn.",
            "refusal": "The agent refused to continue.",
        }
        await sink.turn_failed(reasons.get(str(stop), f"The agent stopped: {stop}"), f"acp.{stop}")

    async def _close_open_calls(self, sink: TurnSink) -> None:
        """A call the agent never finished is shown as failed, not left spinning."""
        for call in list(self._calls.values()):
            if call.call_id in self._announced:
                call.status = "failed"
                await self._complete(call, sink)
        self._calls.clear()

    async def _report_usage(self, result: Mapping[str, Any], sink: TurnSink) -> None:
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
            input_tokens = self._context_used
        output_tokens = usage.get("outputTokens")
        cached = usage.get("cachedReadTokens")
        acp_usage: dict[str, Any] = {}
        for key, value in (
            ("used", self._context_used),
            ("size", self._context_size),
            ("cost", self._cost),
        ):
            if value is not None:
                acp_usage[key] = value
        if input_tokens is None and not isinstance(output_tokens, int) and not acp_usage:
            return
        await sink.usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            cache_read_tokens=cached if isinstance(cached, int) else None,
            model=self._applied_model,
            meta={"acpUsage": acp_usage} if acp_usage else None,
        )


class AcpProvider:
    """One host, one provider: an ACP agent command, rooted at the served folders."""

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
        #: Published once: the host puts `AgentInfo` on the root channel when
        #: it starts, and has no way yet to say it changed.
        self._models = self.current_models()
        self._info = AgentInfo(
            provider=self.provider_id,
            display_name=self._display_name,
            description=self._description,
            models=self._models,
        )
        self._by_chat: weakref.WeakValueDictionary[str, AcpSession] = weakref.WeakValueDictionary()
        self._models_stale = False

    @property
    def agent(self) -> AgentInfo:
        return self._info

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

    def _models_changed(self) -> None:
        """The seam for a live model list.

        Called whenever what the agent reported might change the picker. The
        host publishes `AgentInfo` once, at start, and offers no way for a
        provider to republish it (`root/agentsChanged`) yet -- so for now a
        change is only logged, once, and seen after a restart, through the
        catalogue file. With that host API, publish `current_models()` here.
        """
        if self._models_stale or self.current_models() == self._models:
            return
        self._models_stale = True
        log.info(
            "what %s reported changes its model list; clients see it after the host restarts",
            self.provider_id,
        )

    def _session(self, context: AgentSessionContext, **kwargs: Any) -> AcpSession:
        session = AcpSession(
            context,
            roots=self.roots,
            spec=self.spec,
            connect=self._connect,
            default_model=self.default_model,
            catalogue=self.catalogue,
            on_models_changed=self._models_changed,
            **kwargs,
        )
        self._by_chat[context.chat_uri] = session
        return session

    async def create_session(self, context: AgentSessionContext) -> AcpSession:
        return self._session(context, config_properties=self._config_properties())

    async def resume_session(self, context: AgentSessionContext) -> AcpSession:
        state = context.resume_state or {}
        session_id = state.get("acpSessionId")
        session = self._session(
            context,
            acp_session_id=session_id if isinstance(session_id, str) else None,
            directory=Path(cwd) if isinstance(cwd := state.get("cwd"), str) else None,
        )
        if isinstance(model := state.get("model"), str):
            session.model = model
        return session

    async def resume_state_of(self, session: Any) -> Mapping[str, Any] | None:
        if isinstance(session, AcpSession) and session.acp_session_id:
            state: dict[str, Any] = {"acpSessionId": session.acp_session_id}
            if session.directory is not None:
                state["cwd"] = str(session.directory)
            if session.model:
                state["model"] = session.model
            return state
        return None

    # -- ConfiguresSessions ------------------------------------------------------

    def _config_properties(self) -> dict[str, dict[str, Any]]:
        """The agent's options as session config, as it reported them last.

        The model option is left to the model picker when there is one.
        """
        return self.catalogue.options.properties(
            with_model=not self._models, pinned=self.spec.config_options
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
        """Slash commands: the session's own, or what the agent offered last.

        A session's agent starts on its first turn, so until then the commands
        are the ones it reported most recently -- the same agent's, so the
        same names, short of a project's own.
        """
        if request.kind not in ("", "userMessage"):
            return ()
        session = self._by_chat.get(request.chat)
        commands = session.commands if session is not None else None
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
