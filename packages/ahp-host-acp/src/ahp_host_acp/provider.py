"""The adapter: one ACP agent process per AHP session.

Each session spawns the configured agent command (`openclaw acp`, or any
other ACP agent), opens one ACP session in it, and translates:

- `session/prompt` <- a user turn; its streamed `session/update`s become the
  host's neutral `TurnSink` events (text, reasoning, tool calls, usage);
- `session/request_permission` -> the host's `confirm_tool_call`, which is
  where a client shows an approval prompt;
- a stop -> `session/cancel`.

What this adapter cannot do is decide *which* calls need approval: the agent
asks, or it does not. See the README's Security section.

Models: an agent with a `model` session config option (opencode) is switched
with `session/set_config_option`; one that reports ACP session models, with
`session/set_model`. One that does neither (OpenClaw's bridge) can be given a
`model_command`, a prompt template such as ``/model {model} -s`` sent as its
own turn whenever the picked model changes; its reply is not shown.

Sessions are resumable: the ACP session id is the resume state, restored with
`session/resume` (or `session/load`) when the agent offers it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from ahp_host.provider.base import (
    AgentInfo,
    AgentSessionContext,
    ModelInfo,
    ToolConfirmation,
    TurnSink,
    UserMessage,
)

from ahp_host_acp import jsonrpc
from ahp_host_acp.jsonrpc import (
    AcpConnection,
    AcpError,
    AgentExitedError,
    MethodNotFoundError,
)
from ahp_host_acp.paths import directory_of
from ahp_host_acp.roots import Roots, as_roots
from ahp_host_acp.tools import ToolCall, text_of

log = logging.getLogger(__name__)

PROTOCOL_VERSION: Final = 1
PROVIDER_ID: Final = "acp"
DEFAULT_DESCRIPTION: Final = "An Agent Client Protocol agent, running on this machine."
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
    #: ACP session config options set on every session it opens, by id:
    #: OpenClaw's `thought_level`, for one.
    config_options: Mapping[str, str] = field(default_factory=dict)


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
        #: The id of the agent's model config option (`category: "model"`), if any.
        self._model_option: str | None = None
        self._conn: AcpConnection | None = None
        self._capabilities: Mapping[str, Any] = {}
        self._sink: TurnSink | None = None
        #: While true, updates are dropped: a model switch's reply, or history
        #: an agent replays on `session/load`.
        self._quiet = False
        self._calls: dict[str, ToolCall] = {}
        self._announced: set[str] = set()
        self._context_used: int | None = None
        self._inflight: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()

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
            await self._open_session(conn, cwd)
            await self._set_config_options(conn)
        except BaseException:
            await conn.aclose()
            raise
        self._conn = conn
        return conn

    async def _set_config_options(self, conn: AcpConnection) -> None:
        """Apply the configured options. One the agent refuses is logged, not fatal."""
        for config_id, value in self._spec.config_options.items():
            try:
                await conn.request(
                    "session/set_config_option",
                    {"sessionId": self.acp_session_id, "configId": config_id, "value": value},
                )
            except AgentExitedError:
                raise
            except AcpError as exc:
                log.warning("the agent refused config option %s=%s: %s", config_id, value, exc)

    async def _open_session(self, conn: AcpConnection, cwd: Path) -> None:
        base = {"cwd": str(cwd), "mcpServers": []}
        if self.acp_session_id is not None:
            if await self._reattach(conn, base):
                return
            log.warning("could not reopen ACP session %s; starting a new one", self.acp_session_id)
        result = _mapping(await conn.request("session/new", base))
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpError("the agent's session/new returned no sessionId")
        self.acp_session_id = session_id
        self._note_models(result)
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
        self._note_models(_mapping(result))
        # The agent's own model may have survived, but not provably: switch again.
        self._applied_model = None
        return True

    def _note_models(self, result: Mapping[str, Any]) -> None:
        """How this agent switches models, as far as its answer says.

        Newer agents (opencode) offer the model as a session config option of
        category `model`; older ones report `models` for `session/set_model`.
        An answer that says neither (some agents' `session/resume`) keeps what
        was known.
        """
        if "models" in result:
            self._native_models = bool(_mapping(result.get("models")).get("availableModels"))
        options = result.get("configOptions")
        if isinstance(options, list):
            self._model_option = next(
                (
                    str(option["id"])
                    for option in options
                    if isinstance(option, Mapping)
                    and option.get("category") == "model"
                    and isinstance(option.get("id"), str)
                ),
                None,
            )

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

    # -- agent -> client -------------------------------------------------------

    async def _on_notification(self, method: str, params: Any) -> None:
        if method != "session/update":
            log.debug("ignoring ACP notification %s", method)
            return
        params = _mapping(params)
        if params.get("sessionId") != self.acp_session_id:
            return
        update = _mapping(params.get("update"))
        kind = update.get("sessionUpdate")
        if kind == "usage_update":
            used = update.get("used")
            if isinstance(used, int):
                self._context_used = used
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
            await conn.request(
                "session/set_config_option",
                {"sessionId": self.acp_session_id, "configId": self._model_option, "value": wanted},
            )
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
        usage = _mapping(result.get("usage"))
        input_tokens = usage.get("inputTokens")
        if not isinstance(input_tokens, int):
            # No per-turn usage: the agent's context size is the best gauge.
            input_tokens = self._context_used
        output_tokens = usage.get("outputTokens")
        cached = usage.get("cachedReadTokens")
        if input_tokens is None and not isinstance(output_tokens, int):
            return
        await sink.usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            cache_read_tokens=cached if isinstance(cached, int) else None,
            model=self._applied_model,
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
        self._models = tuple(models)
        self._connect: Connector = connect or AcpConnection.spawn

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name=self._display_name,
            description=self._description,
            models=self._models,
        )

    @property
    def default_model(self) -> str | None:
        return self._models[0].id if self._models else None

    def _session(self, context: AgentSessionContext, **kwargs: Any) -> AcpSession:
        return AcpSession(
            context,
            roots=self.roots,
            spec=self.spec,
            connect=self._connect,
            default_model=self.default_model,
            **kwargs,
        )

    async def create_session(self, context: AgentSessionContext) -> AcpSession:
        return self._session(context)

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
