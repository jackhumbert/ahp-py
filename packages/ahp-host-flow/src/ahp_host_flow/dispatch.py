"""`ProviderDispatcher`: each `agent()` call as a worker chat on an inner provider (plan §4.4)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import itertools
import json
import logging
import math
import mimetypes
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from ahp_host import TurnSink
from ahp_host.core.structured import StructuredTurn
from ahp_host.core.turn import ActionTurnSink
from ahp_host.provider.base import (
    AgentProvider,
    AgentSession,
    AgentSessionContext,
    ClientToolCall,
    FollowsActiveClients,
    IdentifiesTurn,
    InputQuestion,
    InputRequest,
    ModelSelection,
    ProviderChat,
    ReconfiguresChats,
    ReconfiguresSessions,
    ReopensToolChats,
    ResumableAgentProvider,
    ResumesTurns,
    ReturnsStructuredOutput,
    SessionPublisher,
    ToolConfirmation,
    UserMessage,
)
from ahp_protocol.ext import flow
from ahp_protocol.ext.structured import OUTPUT_SCHEMA_META_KEY

from ahp_host_flow import catalog
from ahp_host_flow.approvals import Approver
from ahp_host_flow.bridge import MaskingSink, ScopedPublisher, TurnTee, Watchdog
from ahp_host_flow.clienttools import ClientTools
from ahp_host_flow.config import (
    APPROVAL_KEY,
    APPROVAL_MODES,
    EFFORT_KEY,
    EFFORT_LEVELS,
    ConfigError,
    FlowConfig,
    ProviderConfig,
    make_redactor,
)
from ahp_host_flow.engine import (
    PROVIDER_LOAD,
    AgentCall,
    AgentResult,
    Dispatcher,
    DispatchError,
    DispatchHandle,
    ErrorKind,
    Redactor,
    RemoteAnswer,
    RemoteConfirmation,
    RemoteDecision,
    RemoteOut,
    RemoteQuestion,
    StopCause,
    begin_usage_attempt,
    classify,
    end_usage_attempt,
    preamble_text,
    reattach_handle,
    report_dispatch,
    schema_error,
    stop_cause,
    usage_reporter,
    with_preamble,
)
from ahp_host_flow.engine.dispatcher import _STOP_CAUSE
from ahp_host_flow.engine.remote import RemoteToolError
from ahp_host_flow.limits import ProviderSlotsRegistry
from ahp_host_flow.routing import fallback_chain, route, x_opts
from ahp_host_flow.worktree import (
    Worktree,
    WorktreeChangesets,
    WorktreeError,
    commit_subject,
    create_worktree,
    release_worktree,
    worktree_path,
)

_log = logging.getLogger(__name__)

TITLE_MAX: Final = 60
# Bounded grace for an inner session that honours `cancel` after the run is aborted.
ABORT_GRACE: Final = 5.0
# How often the session's `inputNeeded` is read for the steps in flight, in seconds.
WAIT_POLL: Final = 0.05
#: Which attempt of a call the dispatcher below is running (0 first); `RoutingDispatcher` sets it
#: per `retry_empty` attempt and per fallback provider so each attempt gets its own main-chat
#: tool call id.
_ATTEMPT: contextvars.ContextVar[int] = contextvars.ContextVar("flow_attempt", default=0)
#: The provider a fallback attempt runs on, in place of the call's route (None: route as usual),
#: so the step descriptor names the provider actually used.
_ROUTE_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "flow_route_override", default=None
)
#: Error kinds another provider might cure; a transient 'failed' walks too.
_FALLBACK_KINDS: Final = frozenset({"unavailable", "rate_limited", "timeout", "empty"})


class FlowAgentError(DispatchError):
    """An `agent()` call failed (its worker turn errored or could not start)."""

    def __init__(
        self, message: str, *, kind: ErrorKind = "failed", transient: bool = False
    ) -> None:
        super().__init__(kind, transient, message)


def worker_title(call: AgentCall) -> str:
    """The worker chat's title: the label, else the prompt's first line, truncated."""
    if isinstance(call.label, str) and call.label.strip():
        title = call.label.strip()
    else:
        lines = call.prompt.strip().splitlines()
        title = lines[0].strip() if lines else ""
    if not title:
        return "agent()"
    if len(title) > TITLE_MAX:
        return title[: TITLE_MAX - 1] + "…"
    return title


def named_chat(opts: Mapping[str, Any]) -> str | None:
    """`opts.x.chat`, the name of a reused worker chat; None when absent."""
    x = x_opts(opts)
    if "chat" not in x:
        return None
    value = x["chat"]
    if not (isinstance(value, str) and value):
        raise FlowAgentError("opts.x.chat must be a non-empty string")
    return value


def call_directory(opts: Mapping[str, Any], roots: Sequence[Path]) -> Path | None:
    """`opts.x.cwd` as a resolved directory inside the run's working directories, else None."""
    x = x_opts(opts)
    if "cwd" not in x:
        return None
    value = x["cwd"]
    if not (isinstance(value, str) and value):
        raise FlowAgentError("opts.x.cwd must be a non-empty string")
    p = catalog.directory_of(value)
    if p is None:
        raise FlowAgentError(f"opts.x.cwd: {value} is not a local path")
    if not p.is_absolute():
        if not roots:
            raise FlowAgentError("opts.x.cwd: needs a local working directory")
        p = roots[0] / p
    try:
        resolved = p.resolve(strict=True)
    except OSError as exc:
        raise FlowAgentError(f"opts.x.cwd: {value} does not exist") from exc
    if not resolved.is_dir():
        raise FlowAgentError(f"opts.x.cwd: {value} is not a directory")
    if not any(resolved.is_relative_to(r.resolve()) for r in roots):
        raise FlowAgentError(f"opts.x.cwd: {value} is outside the run's working directories")
    return resolved


def message_attachments(opts: Mapping[str, Any], roots: Sequence[Path]) -> list[dict[str, Any]]:
    """`opts.x.attach` as AHP message attachments; raises FlowAgentError on a bad entry.

    A file becomes a MessageResourceAttachment (label, uri, contentType) and a prior result a
    MessageEmbeddedResourceAttachment (label, base64 JSON data, contentType). A file is resolved
    (symlinks followed) and must be a regular file inside one of the run's working directories.
    """
    x = x_opts(opts)
    if "attach" not in x:
        return []
    items = x["attach"]
    if not isinstance(items, list):
        raise FlowAgentError("opts.x.attach must be an array")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise FlowAgentError(f"opts.x.attach[{i}]: kind must be 'file' or 'result'")
        label = item.get("label")
        if "label" in item and not isinstance(label, str):
            raise FlowAgentError(f"opts.x.attach[{i}]: label must be a string")
        kind = item.get("kind")
        if kind == "file":
            path = item.get("path")
            if not isinstance(path, str):
                raise FlowAgentError(f"opts.x.attach[{i}]: a file needs a path")
            p = Path(path)
            if not p.is_absolute():
                if not roots:
                    raise FlowAgentError(f"opts.x.attach[{i}]: needs a local working directory")
                p = roots[0] / p
            try:
                resolved = p.resolve(strict=True)
            except OSError as exc:
                raise FlowAgentError(f"opts.x.attach[{i}]: {path} does not exist") from exc
            if not resolved.is_file() or not any(
                resolved.is_relative_to(r.resolve()) for r in roots
            ):
                raise FlowAgentError(
                    f"opts.x.attach[{i}]: {path} is outside the run's working directories"
                )
            attachment: dict[str, Any] = {
                "type": "resource",
                "uri": resolved.as_uri(),
                "label": label or resolved.name,
            }
            content_type = mimetypes.guess_type(resolved.name)[0]
            if content_type is not None:
                attachment["contentType"] = content_type
            out.append(attachment)
        elif kind == "result":
            try:
                text = json.dumps(item.get("value"), ensure_ascii=False, allow_nan=False)
            except ValueError as exc:
                raise FlowAgentError(f"opts.x.attach[{i}]: {exc}") from exc
            out.append(
                {
                    "type": "embeddedResource",
                    "label": label or f"result {i}",
                    "contentType": "application/json",
                    "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                }
            )
        else:
            raise FlowAgentError(f"opts.x.attach[{i}]: kind must be 'file' or 'result'")
    return out


def inner_config(
    spec: ProviderConfig, run_values: Mapping[str, Any], opts: Mapping[str, Any]
) -> dict[str, Any]:
    """The inner session's config: approval mode and effort, each only when known."""
    mode = run_values.get(APPROVAL_KEY)
    if not (isinstance(mode, str) and mode in APPROVAL_MODES):
        mode = spec.approval_mode
    effort = opts.get("effort")
    if not (isinstance(effort, str) and effort in EFFORT_LEVELS):
        effort = run_values.get(EFFORT_KEY)
        if not (isinstance(effort, str) and effort in EFFORT_LEVELS):
            effort = spec.effort
    config: dict[str, Any] = {}
    if mode is not None:
        config[APPROVAL_KEY] = mode
    if effort is not None:
        config[EFFORT_KEY] = effort
    return config


def model_for(
    spec: ProviderConfig,
    provider: AgentProvider,
    opts: Mapping[str, Any],
    run_model: str | None,
) -> str | None:
    """The model id for a call: `opts.model` through the alias table, else the run's model."""
    alias = opts.get("model")
    if isinstance(alias, str):
        return spec.models.get(alias, alias)
    if run_model is None:
        return None
    if run_model in spec.models.values():
        return run_model
    for model in provider.agent.models:
        model_id = model.get("id") if isinstance(model, Mapping) else model.id
        if model_id == run_model:
            return run_model
    return None


async def forward_overlay(inner: AgentSession, chat: str, values: Mapping[str, Any]) -> None:
    """Hand a worker chat's config overlay to its inner session, as far as it takes one."""
    if isinstance(inner, ReconfiguresChats):
        await inner.chat_config_changed(chat, values)
    elif isinstance(inner, ReconfiguresSessions):
        await inner.config_changed(values)


@dataclass
class _Worker:
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    aborting: bool = False
    skipped: bool = False
    #: The idle watchdog expired and cancelled the turn.
    timed_out: bool = False
    #: The call cancelled the turn's task itself, after the inner session's grace ran out.
    reaped: bool = False
    started: bool = False
    error: str | None = None
    kind: ErrorKind = "failed"
    transient: bool = False
    #: The turn's cost in USD when the inner agent reported one.
    cost_usd: float | None = None
    #: Set when the host has ended the turn (`done` is set a little before).
    over: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _NamedChat:
    """A named worker chat (`opts.x.chat`) and the inner session its calls share.

    Calls replay from the journal by key, never through this chat. After a restart the first
    live call rebuilds the chat's session from the resume state `DurableWorkers` kept when the
    provider resumes sessions, so the inner agent still remembers the earlier turns; otherwise
    it recreates the chat and the session, and earlier turns exist only in the replay cache.
    """

    provider: str
    worker: ProviderChat
    inner: AgentSession
    title: str
    #: The inner session's config (approval mode, effort), fixed by the first call.
    config: Mapping[str, Any] = field(default_factory=dict)
    #: The folder (`opts.x.cwd`) the inner session was created in, fixed by the first call.
    folder: Path | None = None
    #: The session's running cost (ACP's `acpUsage.cost`) after the last call, so the next
    #: call counts only what it adds.
    session_cost: float = 0.0


@dataclass
class DurableWorkers:
    """What a run keeps of its worker chats so a restarted host can pick them up again.

    Part of the run's entry in the flow resume state; every key is optional there.
    """

    #: Named chats (`opts.x.chat`) by name: `{'provider', 'workerChat', 'title', 'config',
    #: 'folder', 'innerState'}`, written at the end of each turn.
    named: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Worker turns in flight by worker chat (filled by the in-flight recording).
    inflight: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The named chats the cut-off run left, consumed as their names are rebuilt.
    restored_named: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The turns the cut-off run left in flight, consumed as they are picked up again.
    restored_inflight: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.named:
            out["namedChats"] = self.named
        if self.inflight:
            out["inflight"] = self.inflight
        return out


class _TeeSession:
    """Presents an inner session to `StructuredTurn` while its turns write to a `TurnTee`."""

    def __init__(self, inner: AgentSession, tee: TurnTee) -> None:
        self.inner = inner
        self.tee = tee

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        try:
            await self.inner.send_user_message(message, cast(TurnSink, self.tee))
        finally:
            # The structured turn's text fallback reads the whole (masked) reply.
            await self.tee.flush()

    async def cancel(self, reason: str | None = None) -> None:
        await self.inner.cancel(reason)

    async def aclose(self) -> None:
        await self.inner.aclose()

    def offers_host_tools(self) -> bool:
        return isinstance(self.inner, ReturnsStructuredOutput) and self.inner.offers_host_tools()


def waiting_for(entries: object, chat: str) -> list[dict[str, str]]:
    """The `inputNeeded` entries of `chat` a user must answer, as step `waiting` entries."""
    found: list[dict[str, str]] = []
    if not isinstance(entries, list):
        return found
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("chat") != chat:
            continue
        kind = entry.get("kind")
        # The reducer's `_awaits_user` rule: a client-executed tool is not a user prompt.
        if not isinstance(kind, str) or kind == "toolClientExecution":
            continue
        item = {"kind": kind, "chat": chat}
        if item not in found:
            found.append(item)
    return found


class StepDispatcher(ABC):
    """The part of a dispatcher every `agent()` call shares: the main chat's `agent` tool call."""

    reports_dispatch: bool = True

    def __init__(
        self,
        *,
        config: FlowConfig,
        sink: TurnSink,
        publisher: SessionPublisher,
        chat: str,
        run_values: Callable[[], Mapping[str, Any]] = lambda: {},
        redactor: Redactor | None = None,
        on_log: Callable[[str], None] | None = None,
        call_id_prefix: str = "agent",
        session_view: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.sink = sink
        self.publisher = publisher
        self.chat = chat
        self.run_values = run_values
        self.redactor = redactor if redactor is not None else make_redactor(config.redact)
        self.on_log = on_log
        #: Prefixes each call's tool call id; a continued run uses a fresh one so its ids
        #: never collide with the ones the interrupted turn already published.
        self.call_id_prefix = call_id_prefix
        #: The run's id, set once the run is journaled; `isolation: 'worktree'` needs it.
        self.run_id: str | None = None
        #: Calls whose tool call this dispatcher has already completed.
        self._completed: set[str] = set()
        #: The `stepStarted` descriptor of each call in flight (typed progress, plan §4.7).
        self._steps: dict[str, dict[str, Any]] = {}
        #: The session's state when the publisher can show it; `inputNeeded` drives `awaitingInput`.
        self.session_view = session_view
        #: The link item of each call whose worker chat is open (what `_watch_loop` republishes).
        self._links: dict[str, dict[str, Any]] = {}
        #: The `waiting` entries last published for each call.
        self._shown_waiting: dict[str, list[dict[str, str]]] = {}
        self._watcher: asyncio.Task[None] | None = None

    def _watch(self, call_id: str, link_item: dict[str, Any]) -> None:
        """Start following the session's `inputNeeded` for a call whose worker chat just opened."""
        self._links[call_id] = link_item
        if self.session_view is not None and (self._watcher is None or self._watcher.done()):
            self._watcher = asyncio.create_task(self._watch_loop())

    async def _watch_loop(self) -> None:
        while self._links:
            assert self.session_view is not None
            entries = self.session_view().get("inputNeeded")
            for call_id, link in list(self._links.items()):
                started = self._steps.get(call_id)
                if started is None or call_id in self._completed:
                    continue
                chat = started.get("chat")
                if not isinstance(chat, str):
                    continue
                waiting = waiting_for(entries, chat)
                if waiting == self._shown_waiting.get(call_id, []):
                    continue
                self._shown_waiting[call_id] = waiting
                # An empty list publishes the plain step, which clears `awaitingInput`.
                with contextlib.suppress(Exception):
                    await self.sink.tool_call_output(
                        call_id, [link], meta=flow.meta(flow.step(started, waiting=waiting))
                    )
            await asyncio.sleep(WAIT_POLL)

    def _note(self, line: str) -> None:
        if self.on_log is not None:
            self.on_log(line)
        else:
            _log.info("%s", line)

    def _call_limit(self, call: AgentCall, name: str) -> tuple[float | None, str]:
        """Seconds one call may run and where the limit came from: `opts.x.timeout`, else the
        provider's `call_timeout`; None means no limit."""
        x = x_opts(call.opts)
        if "timeout" in x:
            value = x["timeout"]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise FlowAgentError("opts.x.timeout must be a positive number of seconds")
            return float(value), "opts.x.timeout"
        spec = self.config.providers.get(name)
        return (None if spec is None else spec.call_timeout), "call_timeout"

    async def run_agent(self, call: AgentCall) -> AgentResult:
        name = _ROUTE_OVERRIDE.get() or route(self.config.routing, call)
        limit, limit_source = self._call_limit(call, name)
        title = worker_title(call)
        call_id = f"{self.call_id_prefix}-{call.ordinal}"
        if attempt := _ATTEMPT.get():
            call_id += f"-a{attempt}"
        started = flow.step_started(
            ordinal=call.ordinal,
            label=call.label,
            phase=call.phase,
            chat=None,
            model=None,
            provider=name,
        )
        self._steps[call_id] = started
        await self.sink.tool_call_started(
            call_id,
            "agent",
            {"prompt": call.prompt, "opts": dict(call.opts)},
            display_name=title,
            meta=flow.meta(flow.step(started)),
        )
        # The timeout scope goes on the call's stop cause, so a dispatcher cancelled by it can
        # tell its own stop (stop the node session) from the host stopping (leave it running).
        cause = stop_cause()
        cause_token = None
        if cause is None:
            cause = StopCause()
            cause_token = _STOP_CAUSE.set(cause)
        outer_timeout = cause.timeout
        try:
            try:
                # On expiry the cancellation reaches `_run_call`, which stops the worker turn
                # (the inner session's cancel, the remote chat's, the node session's).
                async with asyncio.timeout(limit) as scope:
                    cause.timeout = scope
                    return await self._run_call(call, name, title, call_id)
            except TimeoutError:
                if not scope.expired():
                    raise
                raise FlowAgentError(
                    f"agent() #{call.ordinal}: provider {name!r} took longer than "
                    f"{limit:g}s ({limit_source})",
                    kind="timeout",
                    transient=True,
                ) from None
            finally:
                cause.timeout = outer_timeout
        except asyncio.CancelledError:
            # The Scheduler's hard cap stopped the call: the tool call ends as an error naming
            # the budget, not left `running` (an abort or the host stopping ends the turn).
            if cause.reason is not None and call_id not in self._completed:
                stopped = self.redactor.mask(f"stopped: {cause.reason}")
                with contextlib.suppress(Exception):
                    await self._complete(
                        call_id,
                        {"content": [{"type": "text", "text": f"agent() {stopped}"}]},
                        success=False,
                        message="agent() stopped",
                        finished=flow.step_finished(
                            status="error",
                            result=None,
                            input_tokens=0,
                            output_tokens=0,
                            error=stopped,
                        ),
                    )
            raise
        except Exception as exc:
            # Every way out reports the call: a tool call left `running` would
            # show a call that is long gone. (A skip and a result complete it
            # themselves, and an abort cancels the turn the call is shown in.)
            if call_id not in self._completed:
                with contextlib.suppress(Exception):
                    await self._complete(
                        call_id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": self.redactor.mask(f"agent() failed: {exc}"),
                                }
                            ]
                        },
                        success=False,
                        message="agent() failed",
                        finished=flow.step_finished(
                            status="error",
                            result=None,
                            input_tokens=0,
                            output_tokens=0,
                            error=self.redactor.mask(str(exc)),
                        ),
                    )
            raise
        finally:
            if cause_token is not None:
                _STOP_CAUSE.reset(cause_token)
            self._completed.discard(call_id)
            self._steps.pop(call_id, None)
            self._links.pop(call_id, None)
            self._shown_waiting.pop(call_id, None)
            if not self._links and self._watcher is not None and not self._watcher.done():
                self._watcher.cancel()

    async def _complete(
        self,
        call_id: str,
        content: Mapping[str, Any],
        *,
        success: bool,
        message: str,
        finished: Mapping[str, Any],
    ) -> None:
        self._completed.add(call_id)
        # The reducer spreads a result's fields onto the tool call, so its `_meta`
        # carries the whole descriptor.
        result = {**content, "_meta": flow.meta(flow.step(self._steps[call_id], finished))}
        await self.sink.tool_call_completed(
            call_id, result, success=success, past_tense_message=message
        )

    @abstractmethod
    async def _run_call(
        self, call: AgentCall, name: str, title: str, call_id: str
    ) -> AgentResult: ...


class ProviderDispatcher(StepDispatcher):
    """Runs each `agent()` call as a turn of an inner provider session in its own worker chat."""

    def __init__(
        self,
        *,
        config: FlowConfig,
        providers: Mapping[str, AgentProvider],
        sink: TurnSink,
        publisher: SessionPublisher,
        chat: str,
        session_uri: str,
        working_directories: Sequence[str] = (),
        run_values: Callable[[], Mapping[str, Any]] = lambda: {},
        run_model: str | None = None,
        workers: MutableMapping[str, AgentSession] | None = None,
        pending_overlays: MutableMapping[str, dict[str, Any]] | None = None,
        redactor: Redactor | None = None,
        on_log: Callable[[str], None] | None = None,
        call_id_prefix: str = "agent",
        changesets: WorktreeChangesets | None = None,
        worker_providers: MutableMapping[str, str] | None = None,
        session_view: Callable[[], Mapping[str, Any]] | None = None,
        principal: str | None = None,
        client_tools: ClientTools | None = None,
        durable: DurableWorkers | None = None,
        aborted: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(
            config=config,
            sink=sink,
            publisher=publisher,
            chat=chat,
            run_values=run_values,
            redactor=redactor,
            on_log=on_log,
            call_id_prefix=call_id_prefix,
            session_view=session_view,
        )
        #: Where the named chats are recorded for a restart; None keeps nothing.
        self.durable = durable
        #: True once the run was aborted, so a cancelled worker turn is not taken for one a host
        #: shutdown cut off (the run's `AbortSignal`, as `FleetDispatcher(aborted=...)`).
        self.aborted = aborted
        #: The session's client tools the workers may use; None offers none.
        self.client_tools = client_tools
        #: Who the run is for; the `persons` of an approval rule match it.
        self.principal = principal
        #: Publishes a kept worktree as a changeset on its worker chat; None leaves it unpublished.
        self.changesets = changesets
        self.providers = providers
        self.session_uri = session_uri
        self.working_directories = tuple(working_directories)
        self.run_model = run_model
        self.workers: MutableMapping[str, AgentSession] = workers if workers is not None else {}
        #: Overlays that arrived for a worker chat before its inner session existed.
        self.pending_overlays = pending_overlays
        #: The provider name of each live worker chat (steering needs its profile).
        self.worker_providers: MutableMapping[str, str] = (
            worker_providers if worker_providers is not None else {}
        )
        #: Named worker chats (`opts.x.chat`) of this run and a lock per name.
        self._named: dict[str, _NamedChat] = {}
        self._named_locks: dict[str, asyncio.Lock] = {}
        #: This run's worker chats whose inner session is in `workers` (a turn is running).
        self._live: set[str] = set()

    async def approval_changed(self, mode: str) -> None:
        """Hand an Approvals change on the run's main chat to the run's running workers."""
        for chat in list(self._live):
            inner = self.workers.get(chat)
            if inner is not None:
                # One worker failing never stops the others.
                with contextlib.suppress(Exception):
                    await forward_overlay(inner, chat, {APPROVAL_KEY: mode})

    def _roots(self) -> list[Path]:
        return [p for w in self.working_directories if (p := catalog.directory_of(w)) is not None]

    async def aclose(self) -> None:
        """Close the named chats' sessions; they live until the run ends."""
        for named in self._named.values():
            with contextlib.suppress(Exception):
                await named.inner.aclose()
        self._named.clear()
        self._named_locks.clear()

    async def _run_call(self, call: AgentCall, name: str, title: str, call_id: str) -> AgentResult:
        try:
            provider = self.providers[name]
        except ConfigError as exc:
            kind, transient = classify(PROVIDER_LOAD)
            _log.warning("agent() #%s: provider %r is unavailable: %s", call.ordinal, name, exc)
            raise FlowAgentError(
                f"provider {name!r} is unavailable: {exc}", kind=kind, transient=transient
            ) from exc
        except KeyError:
            raise FlowAgentError(f"provider {name!r} is not an in-process provider") from None
        opts = call.opts
        effort = opts.get("effort")
        if effort is not None and not (isinstance(effort, str) and effort in EFFORT_LEVELS):
            self._note(
                f"agent() #{call.ordinal}: effort {effort!r} is not one of "
                f"{', '.join(EFFORT_LEVELS)}; ignored"
            )
        chat_name = named_chat(opts)
        if chat_name is not None and opts.get("isolation") == "worktree":
            raise FlowAgentError("opts.x.chat cannot be combined with isolation: 'worktree'")
        attachments = message_attachments(opts, self._roots())
        folder = call_directory(opts, self._roots())
        iso = opts.get("isolation")
        if iso is not None and iso != "worktree":
            self._note(f"agent() #{call.ordinal}: isolation {iso!r} is not supported; ignored")
        wt: Worktree | None = None
        if opts.get("isolation") == "worktree":
            workdir = (
                folder
                if folder is not None
                else next(
                    (
                        d
                        for w in self.working_directories
                        if (d := catalog.directory_of(w)) is not None
                    ),
                    None,
                )
            )
            if workdir is None:
                raise FlowAgentError("isolation 'worktree' needs a local working directory")
            if self.run_id is None:
                raise FlowAgentError("isolation 'worktree' needs a journaled run")
            try:
                wt = await create_worktree(
                    workdir, worktree_path(self.config.state_dir, self.run_id, call.ordinal)
                )
            except WorktreeError as exc:
                raise FlowAgentError(str(exc)) from exc
        #: The worker chat, once opened (empty when setup failed before it).
        opened: list[str] = []
        # Read here: the dispatch task has the handle, the host's turn task does not.
        reattach = reattach_handle()
        try:
            if chat_name is None:
                return await self._run_worker(
                    call,
                    name,
                    title,
                    call_id,
                    provider,
                    wt,
                    opened,
                    chat_name=None,
                    attachments=attachments,
                    folder=folder,
                    reattach=reattach,
                )
            # Calls to one name are serialized: a session runs one turn at a time.
            async with self._named_locks.setdefault(chat_name, asyncio.Lock()):
                return await self._run_worker(
                    call,
                    name,
                    title,
                    call_id,
                    provider,
                    wt,
                    opened,
                    chat_name=chat_name,
                    attachments=attachments,
                    folder=folder,
                )
        finally:
            # Every exit after the worktree exists (a failed or cancelled setup too) releases it.
            if wt is not None:
                await self._release_through_cancel(wt, call, opened[0] if opened else None, title)

    async def _release_through_cancel(
        self, wt: Worktree, call: AgentCall, chat: str | None, title: str
    ) -> None:
        """Release *wt* to the end even if this task is cancelled again meanwhile."""
        releasing = asyncio.ensure_future(self._release(wt, call, chat, title))
        cancelled = False
        while not releasing.done():
            try:
                await asyncio.shield(releasing)
            except asyncio.CancelledError:
                if releasing.cancelled():
                    break
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _release(self, wt: Worktree, call: AgentCall, chat: str | None, title: str) -> None:
        try:
            if await release_worktree(wt):
                published: str | None = None
                attempted = bool(chat and self.changesets)
                failed = False
                if chat and self.changesets:
                    try:
                        published = await self.changesets.publish(
                            wt,
                            self.publisher,
                            chat=chat,
                            title=title,
                            ordinal=call.ordinal,
                            session=self.session_uri,
                            run_id=self.run_id,
                            subject=commit_subject(call.label, call.prompt),
                        )
                    except Exception as exc:
                        failed = True
                        self._note(
                            f"agent() #{call.ordinal}: could not publish the worktree changeset: "
                            f"{exc}"
                        )
                if published:
                    suffix = "; its changeset is on the worker chat"
                elif attempted and not failed:
                    # Changed (a commit that leaves the tree as it was, a mode flip), yet
                    # nothing to review, so no changeset can resolve it. Removing it could
                    # lose a commit, so it stays and the line says how to drop it.
                    suffix = (
                        ", but no reviewable changes; "
                        f"remove with `git worktree remove --force {wt.path}`"
                    )
                else:
                    suffix = ""
                self._note(
                    f"agent() #{call.ordinal}: worktree kept at {wt.path} (it has changes){suffix}"
                )
        except (WorktreeError, OSError) as exc:
            self._note(f"agent() #{call.ordinal}: could not remove worktree {wt.path}: {exc}")

    async def _record_named(
        self, chat_name: str, named: _NamedChat, inner: AgentSession, provider: AgentProvider
    ) -> None:
        """Keep *named* in the durable state, with the inner session's resume state."""
        if self.durable is None:
            return
        inner_state: Mapping[str, Any] | None = None
        if isinstance(provider, ResumableAgentProvider):
            try:
                inner_state = await provider.resume_state_of(inner)
            except Exception:
                inner_state = None
        self.durable.named[chat_name] = {
            "provider": named.provider,
            "workerChat": named.worker.resource,
            "title": named.title,
            "config": dict(named.config),
            "folder": str(named.folder) if named.folder is not None else None,
            "innerState": dict(inner_state) if inner_state is not None else None,
        }

    async def _rebuild_named(
        self,
        call: AgentCall,
        name: str,
        chat_name: str,
        provider: AgentProvider,
        spec: ProviderConfig,
        opts: Mapping[str, Any],
    ) -> _NamedChat | None:
        """Named chat *chat_name* as the cut-off run left it, its session resumed; else None."""
        if self.durable is None:
            return None
        record = self.durable.restored_named.pop(chat_name, None)
        if record is None:
            return None
        label = f"agent() #{call.ordinal}: opts.x.chat {chat_name!r}"
        worker: ProviderChat | None = None
        # Why the chat is recreated rather than resumed, checked in this order.
        if record["provider"] != name:
            cause = f"it ran on provider {record['provider']!r} before the restart"
        elif not isinstance(provider, ResumableAgentProvider):
            cause = f"provider {name!r} cannot resume its session"
        elif record["innerState"] is None:
            cause = f"provider {name!r} recorded no session state for it"
        else:
            if isinstance(self.publisher, ReopensToolChats):
                worker = self.publisher.worker_chat(record["workerChat"])
            cause = f"its worker chat {record['workerChat']} is gone"
        if worker is None or not isinstance(provider, ResumableAgentProvider):
            self._note(
                f"{label} was recreated after the restart; {cause}, so it starts without the "
                "earlier turns"
            )
            return None
        folder = Path(record["folder"]) if record["folder"] else None
        try:
            client_id, client_tools = (
                self.client_tools.first() if self.client_tools is not None else (None, ())
            )
            inner = await provider.resume_session(
                AgentSessionContext(
                    session_uri=self.session_uri,
                    chat_uri=worker.resource,
                    provider_id=provider.agent.provider,
                    working_directories=(
                        (folder.as_uri(),) if folder is not None else self.working_directories
                    ),
                    model=model_for(spec, provider, opts, self.run_model),
                    config=record["config"],
                    active_client_id=client_id,
                    client_tools=client_tools,
                    publisher=ScopedPublisher(self.publisher, worker.resource),
                    resume_state=record["innerState"],
                )
            )
        except Exception as exc:
            self._note(f"{label} could not resume its session ({exc}); recreating it")
            return None
        rebuilt = _NamedChat(name, worker, inner, record["title"], dict(record["config"]), folder)
        self._named[chat_name] = rebuilt
        self._note(f"{label} resumed its session on provider {name!r} after the restart")
        return rebuilt

    async def _resume_inflight(
        self,
        call: AgentCall,
        name: str,
        provider: AgentProvider,
        spec: ProviderConfig,
        opts: Mapping[str, Any],
        folder: Path | None,
        handle: DispatchHandle,
    ) -> tuple[ProviderChat, AgentSession, str | None] | None:
        """The worker chat and resumed session of the turn a restart cut off; else None.

        The third item is the cut-off turn's id when the session can resume it in place.
        """
        # Only an in-process handle: a fleet or remote one names a session, never a worker chat.
        chat = handle.worker_chat
        if self.durable is None or chat is None or handle.provider != name:
            return None
        record = self.durable.restored_inflight.pop(chat, None)
        self.durable.inflight.pop(chat, None)
        if record is None or record["provider"] != name:
            return None
        if isinstance(opts.get("schema"), Mapping) or opts.get("isolation") == "worktree":
            return None
        if not (
            isinstance(provider, ResumableAgentProvider)
            and isinstance(self.publisher, ReopensToolChats)
        ):
            return None
        old = self.publisher.worker_chat(chat)
        if old is None:
            return None
        label = f"agent() #{call.ordinal}"
        try:
            client_id, client_tools = (
                self.client_tools.first() if self.client_tools is not None else (None, ())
            )
            inner = await provider.resume_session(
                AgentSessionContext(
                    session_uri=self.session_uri,
                    chat_uri=old.resource,
                    provider_id=provider.agent.provider,
                    working_directories=(
                        (folder.as_uri(),) if folder is not None else self.working_directories
                    ),
                    model=model_for(spec, provider, opts, self.run_model),
                    config=inner_config(spec, self.run_values(), opts),
                    active_client_id=client_id,
                    client_tools=client_tools,
                    publisher=ScopedPublisher(self.publisher, old.resource),
                    resume_state=record["innerState"],
                )
            )
        except Exception as exc:
            self._note(f"{label}: could not resume the cut-off turn ({exc}); dispatching it again")
            return None
        if isinstance(inner, ResumesTurns):
            self._note(
                f"{label}: resumed the turn the restart cut off on provider {name!r} in place "
                f"({old.resource})"
            )
            return old, inner, str(record["turnId"])
        self._note(f"{label}: provider {name!r} cannot resume a turn; dispatching it again")
        return old, inner, None

    async def _run_worker(
        self,
        call: AgentCall,
        name: str,
        title: str,
        call_id: str,
        provider: AgentProvider,
        wt: Worktree | None,
        opened: list[str],
        chat_name: str | None = None,
        attachments: Sequence[dict[str, Any]] = (),
        folder: Path | None = None,
        reattach: DispatchHandle | None = None,
    ) -> AgentResult:
        spec = self.config.providers[name]
        opts = call.opts
        # Read here, in the dispatch task: set by the Scheduler's hard cap or the call's timeout
        # before they cancel it.
        cause = stop_cause()
        #: The cut-off call's worker chat, resumed session and (when the session resumes turns)
        #: the cut-off turn's id.
        resumed: tuple[ProviderChat, AgentSession, str | None] | None = None
        if chat_name is None and wt is None and reattach is not None:
            resumed = await self._resume_inflight(
                call, name, provider, spec, opts, folder, reattach
            )
        existing = self._named.get(chat_name) if chat_name else None
        if existing is None and chat_name is not None:
            existing = await self._rebuild_named(call, name, chat_name, provider, spec, opts)
        if existing is not None and existing.provider != name:
            raise FlowAgentError(
                f"opts.x.chat {chat_name!r} runs on provider {existing.provider!r}; "
                f"this call routes to {name!r}"
            )
        if existing is not None:
            worker = existing.worker
            title = existing.title
            # The session was created with the first call's config; only the model is per
            # message, and the approval mode, which the run's Approvals setting changes at any
            # time, is handed to the session as an overlay. A follow-up asking for another
            # effort keeps the first.
            wanted = inner_config(spec, self.run_values(), opts)
            mode = wanted.get(APPROVAL_KEY)
            if mode is not None and mode != existing.config.get(APPROVAL_KEY):
                await forward_overlay(
                    existing.inner, existing.worker.resource, {APPROVAL_KEY: mode}
                )
                existing.config = {**existing.config, APPROVAL_KEY: mode}
            if wanted != dict(existing.config):
                self._note(
                    f"agent() #{call.ordinal}: opts.x.chat {chat_name!r} keeps its first call's "
                    f"config {json.dumps(dict(existing.config), sort_keys=True)}; "
                    f"{json.dumps(wanted, sort_keys=True)} ignored"
                )
            if folder != existing.folder:
                first = str(existing.folder) if existing.folder is not None else "(the run's)"
                later = str(folder) if folder is not None else "(the run's)"
                self._note(
                    f"agent() #{call.ordinal}: opts.x.chat {chat_name!r} keeps its first call's "
                    f"folder {first}; {later} ignored"
                )
        elif resumed is not None:
            worker = resumed[0]
        else:
            worker = await self.publisher.open_tool_chat(
                title,
                tool_call_id=call_id,
                chat=self.chat,
                interactivity="full" if spec.interactive else "read-only",
            )
        opened.append(worker.resource)
        # `worker_chat` only for a call whose inner state may get recorded: the engine hands a
        # continued run the handle of a cut-off call only when it names a place to reattach to.
        report_dispatch(
            DispatchHandle(
                provider=name,
                chat=worker.resource,
                worker_chat=(
                    worker.resource
                    if self.durable is not None
                    and chat_name is None
                    and wt is None
                    and isinstance(provider, ResumableAgentProvider)
                    else None
                ),
            )
        )

        raw_schema = opts.get("schema")
        schema = raw_schema if isinstance(raw_schema, Mapping) else None
        # Only what the inner agent is sent: the worker chat, the journal key and the main
        # chat keep the bare prompt. A follow-up in a named chat (`existing`) goes to a session
        # that already has the preamble from the chat's first message, so it is sent bare.
        text = (
            call.prompt
            if existing is not None
            else with_preamble(call.prompt, preamble_text(spec.preamble))
        )
        if existing is not None:
            # A discarded changeset's reason, queued for this chat: sent, not recorded.
            notes = self.changesets.take_notes(worker.resource) if self.changesets else []
            if notes:
                text = "\n".join(notes) + "\n" + text
        raw: dict[str, Any] = {"text": text, "origin": {"kind": "agent"}}
        if schema:
            raw["_meta"] = {OUTPUT_SCHEMA_META_KEY: dict(schema)}
        if attachments:
            raw["attachments"] = list(attachments)
        model = model_for(spec, provider, opts, self.run_model)
        started = self._steps[call_id]
        started["chat"] = worker.resource
        started["model"] = model
        link_item = {"type": "subagent", "resource": worker.resource, "title": title}
        # Clients can open the child chat while it runs.
        await self.sink.tool_call_output(call_id, [link_item], meta=flow.meta(flow.step(started)))
        self._watch(call_id, link_item)
        message = UserMessage(
            text=text,
            raw=raw,
            model=ModelSelection(id=model) if model else None,
            chat_uri=worker.resource,
        )
        if existing is not None:
            inner = existing.inner
        elif resumed is not None:
            inner = resumed[1]
        else:
            config = inner_config(spec, self.run_values(), opts)
            directories = (
                (wt.cwd.as_uri(),)
                if wt
                else ((folder.as_uri(),) if folder is not None else self.working_directories)
            )
            client_id, client_tools = (
                self.client_tools.first() if self.client_tools is not None else (None, ())
            )
            inner = await provider.create_session(
                AgentSessionContext(
                    session_uri=self.session_uri,
                    chat_uri=worker.resource,
                    provider_id=provider.agent.provider,
                    working_directories=directories,
                    model=model,
                    config=config,
                    active_client_id=client_id,
                    client_tools=client_tools,
                    publisher=ScopedPublisher(self.publisher, worker.resource),
                )
            )
            if chat_name is not None:
                self._named[chat_name] = _NamedChat(name, worker, inner, title, config, folder)
        named = self._named.get(chat_name) if chat_name else None
        durable = self.durable
        inner_state: Mapping[str, Any] | None = None
        resume_turn_id = resumed[2] if resumed is not None else None

        state = _Worker()
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        value: Any = None
        # The turn runs in the host's task: take the hook from this (the scheduler's) context.
        reporter = usage_reporter()

        async def on_waiting(waiting: bool) -> None:
            await self.sink.tool_call_output(
                call_id, [link_item], meta=flow.meta(flow.step(started, awaiting=waiting))
            )

        approver: Approver | None = None
        if self.config.approvals.rules or self.config.policy is not None:
            # A worktree lies outside the working directories, and paths outside the roots
            # are denied.
            roots = ((wt.path,) if wt else ()) + tuple(self._roots())
            if wt is not None:
                cwd = wt.cwd
            else:
                cwd = folder if folder is not None else (roots[0] if roots else Path.cwd())
            approver = Approver(
                self.config.approvals.rules,
                self.config.policy,
                provider=name,
                phase=call.phase,
                principal=self.principal,
                cwd=cwd,
                roots=roots,
                label=f"agent() #{call.ordinal}",
                on_log=self._note,
            )

        def idle_expired() -> None:
            state.timed_out = True
            if state.task is not None:
                state.task.cancel()

        async def run(sink: TurnSink) -> None:
            nonlocal value
            state.started = True
            if durable is not None and inner_state is not None and isinstance(sink, IdentifiesTurn):
                durable.inflight[worker.resource] = {
                    "provider": name,
                    "turnId": sink.turn_id,
                    "innerState": dict(inner_state),
                }
            state.task = asyncio.current_task()
            watchdog: Watchdog | None = None
            if spec.idle_timeout is not None:
                watchdog = Watchdog(spec.idle_timeout, idle_expired)
                watchdog.start()
            tee = TurnTee(
                sink,
                redactor=self.redactor,
                on_waiting=on_waiting if self.session_view is None else None,
                approver=approver,
                on_usage=reporter,
                watchdog=watchdog,
                cost_base=named.session_cost if named is not None else 0.0,
            )
            try:
                if schema is None:
                    if resume_turn_id is not None and isinstance(inner, ResumesTurns):
                        await inner.resume_turn(
                            worker.resource, resume_turn_id, cast(TurnSink, tee)
                        )
                    else:
                        await inner.send_user_message(message, cast(TurnSink, tee))
                    value = self.redactor.mask_value(tee.reply)
                else:
                    if not isinstance(sink, ActionTurnSink):
                        raise FlowAgentError("a structured agent() needs the host's turn sink")
                    turn = StructuredTurn(schema)
                    # The turn publishes the StructuredOutput call and its result itself:
                    # hand it a sink that masks those copies (validation stays unmasked).
                    masking = MaskingSink(sink, self.redactor, tee.flush)
                    await turn.run(
                        _TeeSession(inner, tee),
                        message,
                        cast(ActionTurnSink, masking),
                        is_active=lambda: not tee.failed,
                    )
                    value = self.redactor.mask_value(turn.value)
                if tee.failed:
                    state.error = tee.failure
                    state.kind, state.transient = classify(
                        tee.error_type or "", resumable=tee.resumable, message=tee.failure or ""
                    )
            except asyncio.CancelledError:
                if state.timed_out:
                    with contextlib.suppress(Exception):
                        await inner.cancel("idle timeout")
                elif not state.aborting:
                    state.skipped = True
                    with contextlib.suppress(Exception):
                        await inner.cancel("skipped")
                if not (state.timed_out or state.reaped):
                    raise
                # The call stopped its own turn: a cancelled turn task leaves the worker chat's
                # turn active (only a client's `chat/turnCancelled` ends one), and a named
                # chat's next call could not start a turn. End it as a failed turn instead.
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                with contextlib.suppress(Exception):
                    await tee.turn_failed(
                        "no activity (idle_timeout)" if state.timed_out else "agent() stopped",
                        "flow.stopped",
                    )
            except Exception as exc:
                state.error = self.redactor.mask(str(exc))
                raise
            finally:
                if watchdog is not None:
                    watchdog.stop()
                with contextlib.suppress(Exception):
                    await tee.flush()
                # Requests not yet covered by a turn report count: a stopped turn spent them.
                totals = tee.totals()
                usage["input_tokens"] = totals["input_tokens"]
                usage["output_tokens"] = totals["output_tokens"]
                usage["cache_read_tokens"] = totals["cache_read_tokens"]
                state.cost_usd = tee.cost_usd
                if named is not None and tee.session_cost is not None:
                    named.session_cost = tee.session_cost
                # The attempt's final figures, also when it failed: the Scheduler counts them.
                final: dict[str, Any] = dict(usage)
                if tee.cost_usd is not None:
                    final["cost_usd"] = tee.cost_usd
                reporter(final)
                if named is not None and chat_name is not None and self.durable is not None:
                    await self._record_named(chat_name, named, inner, provider)
                state.done.set()

        def turn_ended() -> None:
            # If `run` never started, the turn was cancelled before its first
            # step and saw no `CancelledError`: this is the only word that the
            # call is over, and it counts as a skip.
            if not state.started:
                state.skipped = True
            state.done.set()
            state.over.set()

        #: Cancelled from outside while the turn ran (a shutdown): its record stays in the store.
        cut_off = False
        #: The worker turn is being started (until then nothing waits on `done` or `over`).
        launched = False
        # From here every way out closes what the setup registered, the inner session too.
        try:
            self.worker_providers[worker.resource] = name
            # An unnamed, schema-less call keeps its session's state while the turn runs, so a
            # restart can resume the turn in place (`ResumesTurns`) rather than send it again.
            if (
                durable is not None
                and chat_name is None
                and schema is None
                and wt is None
                and isinstance(provider, ResumableAgentProvider)
            ):
                try:
                    inner_state = await provider.resume_state_of(inner)
                except Exception:
                    inner_state = None
            self.workers[worker.resource] = inner
            self._live.add(worker.resource)
            if self.client_tools is not None and isinstance(inner, FollowsActiveClients):
                # A new session gets the full list; a named chat's one is refreshed between calls.
                with contextlib.suppress(Exception):
                    await inner.active_clients_changed(self.client_tools.clients())
            pending = (
                self.pending_overlays.pop(worker.resource, None)
                if self.pending_overlays is not None
                else None
            )
            if pending:
                await forward_overlay(inner, worker.resource, pending)
            launched = True
            # `on_end` is the only word of a turn cancelled before `run` starts.
            if not await worker.run_turn(call.prompt, run, on_end=turn_ended):
                raise FlowAgentError("could not start a turn in the worker chat")
            # `run_turn` returns when the turn starts, so wait on our own signal.
            await state.done.wait()
            if chat_name is not None:
                # `run_turn` refuses while a turn runs and `done` is set before the host
                # ends the turn, so the next call would otherwise race the turn end.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(state.over.wait(), ABORT_GRACE)
        except asyncio.CancelledError:
            # The run was aborted (a worker skip cancels only the callback). A host shutdown
            # lands here too, before the store's last write: only then does the turn stay
            # recorded as in flight, so the restarted host can resume it. An abort, a skip, the
            # idle watchdog and a stop the run made (budget, quota, wall time, call timeout) all
            # end the call for good.
            state.aborting = True
            cut_off = not (
                state.skipped
                or state.timed_out
                or self.aborted()
                or (cause is not None and cause.stopped())
            )
            with contextlib.suppress(Exception):
                await inner.cancel("run aborted")
            if launched:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(state.done.wait(), ABORT_GRACE)
            if state.task is not None and not state.task.done():
                # A host shutdown cancels the turn itself; any other stop is this call's own.
                state.reaped = not cut_off
                state.task.cancel()
            if launched and chat_name is not None:
                # The chat outlives this call (a timeout, say): the next call on it must find
                # the turn ended, not still running, or its `run_turn` is refused.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(state.over.wait(), ABORT_GRACE)
            raise
        finally:
            self.workers.pop(worker.resource, None)
            self._live.discard(worker.resource)
            if durable is not None and not cut_off:
                durable.inflight.pop(worker.resource, None)
            self.worker_providers.pop(worker.resource, None)
            if self.pending_overlays is not None:
                self.pending_overlays.pop(worker.resource, None)
            if state.skipped and not state.started:
                with contextlib.suppress(Exception):
                    await inner.cancel("skipped")
            if chat_name is None:
                with contextlib.suppress(Exception):
                    await inner.aclose()

        link = {"content": [link_item]}
        in_tok, out_tok = usage["input_tokens"], usage["output_tokens"]
        if state.skipped:
            finished = flow.step_finished(
                status="skipped", result=None, input_tokens=in_tok, output_tokens=out_tok
            )
            await self._complete(
                call_id, link, success=False, message="agent() skipped", finished=finished
            )
            raise asyncio.CancelledError
        if state.timed_out:
            idle = f"provider {name!r}: no activity for {spec.idle_timeout:g}s (idle_timeout); "
            idle += "stopped the turn"
            finished = flow.step_finished(
                status="error",
                result=None,
                error=idle,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
            await self._complete(
                call_id, link, success=False, message="agent() failed", finished=finished
            )
            raise FlowAgentError(idle, kind="timeout", transient=True)
        if state.error is not None:
            finished = flow.step_finished(
                status="error",
                result=None,
                error=state.error,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
            await self._complete(
                call_id, link, success=False, message="agent() failed", finished=finished
            )
            raise FlowAgentError(state.error, kind=state.kind, transient=state.transient)
        if value is None:
            finished = flow.step_finished(
                status="null", result=None, input_tokens=in_tok, output_tokens=out_tok
            )
        elif isinstance(raw_schema, Mapping) and (bad := schema_error(raw_schema, value)):
            # The engine re-validates identically, so this is what agent() returns.
            finished = flow.step_finished(
                status="error",
                result=None,
                error=bad,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
            await self._complete(
                call_id, link, success=False, message="agent() failed", finished=finished
            )
            return AgentResult(value, usage=usage, cost_usd=state.cost_usd)
        else:
            finished = flow.step_finished(
                status="ok", result=value, input_tokens=in_tok, output_tokens=out_tok
            )
        await self._complete(
            call_id, link, success=True, message="agent() returned", finished=finished
        )
        return AgentResult(value, usage=usage, cost_usd=state.cost_usd)


class _WorkerOut:
    """A remote call's output, written to its worker chat."""

    def __init__(
        self,
        sink: TurnSink,
        provider: str,
        chat: str,
        redactor: Redactor,
        tools: ClientTools | None = None,
    ) -> None:
        self.sink = sink
        self.provider = provider
        self.chat = chat
        self.redactor = redactor
        self.client_tools = tools

    def tools(self) -> list[dict[str, Any]]:
        return self.client_tools.definitions() if self.client_tools is not None else []

    async def run_tool(self, name: str, tool_input: Any, tool_call_id: str) -> Mapping[str, Any]:
        owner = self.client_tools.owner(name) if self.client_tools is not None else None
        if owner is None:
            raise RemoteToolError(f"no client of the run offers {name!r}")
        try:
            result = await self.sink.run_client_tool(
                ClientToolCall(
                    call_id=f"remote-{tool_call_id}",
                    name=name,
                    client_id=owner,
                    tool_input=tool_input,
                    display_name=name,
                )
            )
        except LookupError as exc:
            raise RemoteToolError(str(exc)) from exc
        if not result.accepted:
            raise RemoteToolError(result.reason or f"the client answered {result.response}")
        if isinstance(result.value, Mapping):
            return result.value
        return {
            "success": True,
            "pastTenseMessage": f"Ran {name}",
            "content": [{"type": "text", "text": json.dumps(result.value)}],
        }

    async def linked(self, session_uri: str) -> None:
        await self.sink.text_delta(f"Remote session: {session_uri}\n")
        report_dispatch(DispatchHandle(provider=self.provider, chat=self.chat, session=session_uri))

    async def text(self, delta: str) -> None:
        await self.sink.text_delta(self.redactor.mask(delta))

    # No approval rules apply to relayed remote requests: the worker chat's user decides.
    async def confirm(self, request: RemoteConfirmation) -> RemoteDecision | None:
        tee = TurnTee(self.sink, redactor=self.redactor)
        name = request.tool_name or "tool"
        await tee.tool_call_started(
            request.tool_call_id,
            name,
            request.tool_input,
            display_name=request.tool_name or None,
        )
        outcome = await tee.confirm_tool_call(
            ToolConfirmation(
                call_id=request.tool_call_id,
                name=name,
                invocation_message=request.invocation_message
                or f"Run {request.tool_name or 'a tool'} on the remote host",
                tool_input=request.tool_input,
                confirmation_title=request.confirmation_title,
                read_only=request.read_only,
            )
        )
        await tee.tool_call_completed(
            request.tool_call_id,
            None,
            success=outcome.approved,
            past_tense_message="Approved" if outcome.approved else "Denied",
        )
        suggestion: dict[str, Any] | None = None
        given = outcome.user_suggestion
        if given is not None:
            if isinstance(given.raw, Mapping) and given.raw:
                suggestion = dict(given.raw)
            elif given.text:
                suggestion = {"text": given.text}
        return RemoteDecision(outcome.approved, outcome.reason_message, suggestion)

    async def ask(self, request: RemoteQuestion) -> RemoteAnswer | None:
        tee = TurnTee(self.sink, redactor=self.redactor)
        outcome = await tee.request_input(
            InputRequest(
                message=request.message,
                url=request.url,
                questions=[
                    InputQuestion(
                        id=q["id"],
                        kind=str(q.get("kind", "text")),
                        message=str(q.get("message", "")),
                        options=list(q.get("options") or []),
                        extra={
                            k: v
                            for k, v in q.items()
                            if k not in ("id", "kind", "message", "options")
                        },
                    )
                    for q in request.questions
                    if isinstance(q.get("id"), str)
                ],
            )
        )
        return RemoteAnswer(outcome.response, dict(outcome.answers))


class RemoteWorkerChats(StepDispatcher):
    """Shows each remote `agent()` call as a thin worker chat: the remote session and its text."""

    def __init__(self, *, client_tools: ClientTools | None = None, **options: Any) -> None:
        super().__init__(**options)
        self.client_tools = client_tools
        #: The body of each call in flight, by ordinal; `run` hands it to `_run_call`.
        self._bodies: dict[int, Callable[[RemoteOut, str], Awaitable[AgentResult]]] = {}

    async def run(
        self,
        call: AgentCall,
        provider: str,
        body: Callable[[RemoteOut, str], Awaitable[AgentResult]],
    ) -> AgentResult:
        self._bodies[call.ordinal] = body
        try:
            return await self.run_agent(call)
        finally:
            self._bodies.pop(call.ordinal, None)

    async def _run_call(self, call: AgentCall, name: str, title: str, call_id: str) -> AgentResult:
        body = self._bodies[call.ordinal]
        worker = await self.publisher.open_tool_chat(title, tool_call_id=call_id, chat=self.chat)
        raw_schema = call.opts.get("schema")
        started = self._steps[call_id]
        started["chat"] = worker.resource
        link_item = {"type": "subagent", "resource": worker.resource, "title": title}
        # Clients can open the worker chat while it runs.
        await self.sink.tool_call_output(call_id, [link_item], meta=flow.meta(flow.step(started)))
        self._watch(call_id, link_item)

        state = _Worker()
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        value: Any = None

        async def run(sink: TurnSink) -> None:
            nonlocal value
            state.started = True
            state.task = asyncio.current_task()

            try:
                result = await body(
                    _WorkerOut(sink, name, worker.resource, self.redactor, self.client_tools), title
                )
                for key in usage:
                    count = result.usage.get(key)
                    usage[key] = count if isinstance(count, int) else 0
                await sink.usage(
                    input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]
                )
                value = self.redactor.mask_value(result.value)
            except asyncio.CancelledError:
                if not state.aborting:
                    state.skipped = True
                raise
            except Exception as exc:
                state.error = self.redactor.mask(str(exc))
                if isinstance(exc, DispatchError):
                    state.kind, state.transient = exc.kind, exc.transient
                raise
            finally:
                state.done.set()

        def turn_ended() -> None:
            # If `run` never started, the turn was cancelled before its first step: a skip.
            if not state.started:
                state.skipped = True
            state.done.set()

        try:
            if not await worker.run_turn(call.prompt, run, on_end=turn_ended):
                raise FlowAgentError("could not start a turn in the worker chat")
            await state.done.wait()
        except asyncio.CancelledError:
            # The run was aborted (a worker skip cancels only the callback).
            state.aborting = True
            if state.task is not None and not state.task.done():
                state.task.cancel()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(state.done.wait(), ABORT_GRACE)
            raise

        link = {"content": [link_item]}
        in_tok, out_tok = usage["input_tokens"], usage["output_tokens"]
        if state.skipped:
            finished = flow.step_finished(
                status="skipped", result=None, input_tokens=in_tok, output_tokens=out_tok
            )
            await self._complete(
                call_id, link, success=False, message="agent() skipped", finished=finished
            )
            raise asyncio.CancelledError
        if state.error is not None:
            finished = flow.step_finished(
                status="error",
                result=None,
                error=state.error,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
            await self._complete(
                call_id, link, success=False, message="agent() failed", finished=finished
            )
            raise FlowAgentError(state.error, kind=state.kind, transient=state.transient)
        result = AgentResult(value, usage=usage)
        if value is None:
            finished = flow.step_finished(
                status="null", result=None, input_tokens=in_tok, output_tokens=out_tok
            )
        elif isinstance(raw_schema, Mapping) and (bad := schema_error(raw_schema, value)):
            # The engine re-validates identically, so this is what agent() returns.
            finished = flow.step_finished(
                status="error",
                result=None,
                error=bad,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
            await self._complete(
                call_id, link, success=False, message="agent() failed", finished=finished
            )
            return result
        else:
            finished = flow.step_finished(
                status="ok", result=value, input_tokens=in_tok, output_tokens=out_tok
            )
        await self._complete(
            call_id, link, success=True, message="agent() returned", finished=finished
        )
        return result


class RoutingDispatcher:
    """Routes each `agent()` call to the dispatcher for its provider's kind."""

    reports_dispatch: bool = True

    def __init__(
        self,
        config: FlowConfig,
        *,
        inprocess: Dispatcher,
        fleet: Dispatcher | None = None,
        remote: Callable[[str, ProviderConfig], Dispatcher] | None = None,
        slots: ProviderSlotsRegistry | None = None,
    ) -> None:
        self.config = config
        #: Per-provider concurrency, shared with every other run; held inside this run's own
        #: Scheduler slot, never instead of it.
        self._slots = slots
        self._by_kind: dict[str, Dispatcher | None] = {"inprocess": inprocess, "fleet": fleet}
        #: Makes the dispatcher of a remote provider, on its first call.
        self._remote = remote
        self._remotes: dict[str, Dispatcher] = {}
        self._run_id: str | None = None

    def _inner(self) -> list[Dispatcher]:
        return [
            *(d for d in self._by_kind.values() if d is not None),
            *self._remotes.values(),
        ]

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @run_id.setter
    def run_id(self, value: str | None) -> None:
        self._run_id = value
        for dispatcher in self._inner():
            if hasattr(dispatcher, "run_id"):
                dispatcher.run_id = value

    async def run_agent(self, call: AgentCall) -> AgentResult:
        chain = fallback_chain(self.config, call)
        if chain[0] not in self.config.providers:
            configured = ", ".join(sorted(self.config.providers))
            raise FlowAgentError(
                f"provider {chain[0]!r} is not configured (opts.x.provider); "
                f"configured: {configured}"
            )
        in_process_only = named_chat(call.opts) is not None or "attach" in x_opts(call.opts)
        attempts = itertools.count()
        failed = chain[0]
        error: DispatchError | None = None
        for index, name in enumerate(chain):
            spec = self.config.providers[name]
            if index:
                assert error is not None
                if in_process_only and spec.kind != "inprocess":
                    self._note(
                        f"agent() #{call.ordinal}: fallback to {name} skipped ({name} is a "
                        f"{spec.kind} provider; opts.x.chat/attach need an in-process one)"
                    )
                    continue
                self._note(
                    f"agent() #{call.ordinal}: fell back from {failed} to {name} ({error.kind})"
                )
            token = _ROUTE_OVERRIDE.set(name if index else None)
            try:
                return await self._run_on(name, spec, call, attempts)
            except DispatchError as exc:
                if not (exc.kind in _FALLBACK_KINDS or (exc.kind == "failed" and exc.transient)):
                    raise
                error, failed = exc, name
            finally:
                _ROUTE_OVERRIDE.reset(token)
        assert error is not None
        raise error

    def _note(self, line: str) -> None:
        _log.info("%s", line)

    async def _run_on(
        self,
        name: str,
        spec: ProviderConfig,
        call: AgentCall,
        attempts: Iterator[int],
    ) -> AgentResult:
        """One provider's turn at *call*, with its `retry_empty` attempts."""
        kind = spec.kind
        chat = named_chat(call.opts)
        if (chat is not None or "attach" in x_opts(call.opts)) and kind != "inprocess":
            raise FlowAgentError(
                f"opts.x.{'chat' if chat else 'attach'} needs an in-process provider; "
                f"{name!r} is a {kind} provider"
            )
        dispatcher: Dispatcher | None
        if kind == "remote" and self._remote is not None:
            dispatcher = self._remotes.get(name)
            if dispatcher is None:
                dispatcher = self._remotes[name] = self._remote(name, spec)
                if hasattr(dispatcher, "run_id"):
                    dispatcher.run_id = self._run_id
        else:
            dispatcher = self._by_kind.get(kind)
        if dispatcher is None:
            raise FlowAgentError(f"provider {name!r}: {kind} dispatch is not available here")
        if spec.retry_empty <= 0:
            return await self._attempt(dispatcher, call, name, attempts)
        return await self._run_retrying(dispatcher, call, name, spec.retry_empty, attempts)

    async def _attempt(
        self, dispatcher: Dispatcher, call: AgentCall, name: str, attempts: Iterator[int]
    ) -> AgentResult:
        """One dispatch inside the provider's concurrency slot; a busy provider halves the limit.

        Each dispatch of a call takes the next attempt number, so its tool call id is its own,
        and a usage slot of its own, so the Scheduler counts a failed attempt's usage too.
        """
        token = _ATTEMPT.set(next(attempts))
        reporter, usage_token = begin_usage_attempt()
        try:
            if self._slots is None:
                result = await dispatcher.run_agent(call)
            else:
                slots = self._slots.for_name(name)
                async with slots.hold():
                    try:
                        result = await dispatcher.run_agent(call)
                    except DispatchError as exc:
                        if exc.kind == "rate_limited" or (
                            exc.kind == "unavailable" and exc.transient
                        ):
                            slots.on_busy(self._slots.on_log, exc.kind)
                        raise
            # The attempt's result is its settled usage (it replaces what it reported live).
            settled: dict[str, Any] = dict(result.usage)
            if result.cost_usd is not None:
                settled["cost_usd"] = result.cost_usd
            reporter(settled)
            return result
        finally:
            end_usage_attempt(usage_token)
            _ATTEMPT.reset(token)

    async def _run_retrying(
        self,
        dispatcher: Dispatcher,
        call: AgentCall,
        name: str,
        retries: int,
        attempts: Iterator[int],
    ) -> AgentResult:
        """Dispatches *call* again while its reply is empty, up to *retries* more times.

        The Scheduler sees one result: the last attempt's value, with the usage and cost of every
        attempt summed (one ordinal, one key, one journal record).
        """
        schema = call.opts.get("schema")
        usage: dict[str, int] = {}
        cost: float | None = None
        attempt = 0
        while True:
            result = await self._attempt(dispatcher, call, name, attempts)
            for key, count in result.usage.items():
                usage[key] = usage.get(key, 0) + count
            if result.cost_usd is not None:
                cost = (cost or 0.0) + result.cost_usd
            empty = (
                result.value is None
                if isinstance(schema, Mapping)
                else isinstance(result.value, str) and result.value.strip() == ""
            )
            if not empty:
                return AgentResult(result.value, usage=usage, cost_usd=cost)
            if attempt >= retries:
                raise FlowAgentError(
                    f"agent() #{call.ordinal}: {name} returned an empty reply {retries + 1} times",
                    kind="empty",
                    transient=True,
                )
            attempt += 1
            _log.info(
                "agent() #%d: empty reply from %s; retrying (%d of %d)",
                call.ordinal,
                name,
                attempt,
                retries,
            )

    async def aclose(self) -> None:
        for dispatcher in self._inner():
            close = getattr(dispatcher, "aclose", None)
            if close is not None:
                await close()
