"""`ProviderDispatcher`: each `agent()` call as a worker chat on an inner provider (plan §4.4)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
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
    ModelSelection,
    ProviderChat,
    ReconfiguresChats,
    ReconfiguresSessions,
    ReturnsStructuredOutput,
    SessionPublisher,
    UserMessage,
)
from ahp_protocol.ext import flow
from ahp_protocol.ext.structured import OUTPUT_SCHEMA_META_KEY

from ahp_host_flow import catalog
from ahp_host_flow.bridge import MaskingSink, ScopedPublisher, TurnTee
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
    AgentCall,
    AgentResult,
    Dispatcher,
    DispatchHandle,
    Redactor,
    RemoteOut,
    preamble_text,
    report_dispatch,
    schema_error,
    with_preamble,
)
from ahp_host_flow.routing import route, x_opts
from ahp_host_flow.worktree import (
    Worktree,
    WorktreeChangesets,
    WorktreeError,
    create_worktree,
    release_worktree,
    worktree_path,
)

_log = logging.getLogger(__name__)

TITLE_MAX: Final = 60
# Bounded grace for an inner session that honours `cancel` after the run is aborted.
ABORT_GRACE: Final = 5.0


class FlowAgentError(RuntimeError):
    """An `agent()` call failed (its worker turn errored or could not start)."""


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
    started: bool = False
    error: str | None = None
    #: Set when the host has ended the turn (`done` is set a little before).
    over: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _NamedChat:
    """A named worker chat (`opts.x.chat`) and the inner session its calls share.

    Calls replay from the journal by key, never through this chat: on resume the first live
    call recreates the chat and the session, so earlier turns exist only in the replay cache
    and the inner agent forgets them unless its provider resumes sessions.
    """

    provider: str
    worker: ProviderChat
    inner: AgentSession
    title: str
    #: The inner session's config (approval mode, effort), fixed by the first call.
    config: Mapping[str, Any] = field(default_factory=dict)
    #: The folder (`opts.x.cwd`) the inner session was created in, fixed by the first call.
    folder: Path | None = None


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

    def _note(self, line: str) -> None:
        if self.on_log is not None:
            self.on_log(line)
        else:
            _log.info("%s", line)

    async def run_agent(self, call: AgentCall) -> AgentResult:
        name = route(self.config.routing, call)
        title = worker_title(call)
        call_id = f"{self.call_id_prefix}-{call.ordinal}"
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
        try:
            return await self._run_call(call, name, title, call_id)
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
            self._completed.discard(call_id)
            self._steps.pop(call_id, None)

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
        )
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
            _log.warning("agent() #%s: provider %r is unavailable: %s", call.ordinal, name, exc)
            raise FlowAgentError(f"provider {name!r} is unavailable: {exc}") from exc
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
    ) -> AgentResult:
        spec = self.config.providers[name]
        opts = call.opts
        existing = self._named.get(chat_name) if chat_name else None
        if existing is not None and existing.provider != name:
            raise FlowAgentError(
                f"opts.x.chat {chat_name!r} runs on provider {existing.provider!r}; "
                f"this call routes to {name!r}"
            )
        if existing is not None:
            worker = existing.worker
            title = existing.title
            # The session was created with the first call's config; only the model is per
            # message. A follow-up asking for another effort or approval mode keeps the first.
            wanted = inner_config(spec, self.run_values(), opts)
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
        else:
            worker = await self.publisher.open_tool_chat(
                title,
                tool_call_id=call_id,
                chat=self.chat,
                interactivity="full" if spec.interactive else "read-only",
            )
        opened.append(worker.resource)
        self.worker_providers[worker.resource] = name
        report_dispatch(DispatchHandle(provider=name, chat=worker.resource))

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
        message = UserMessage(
            text=text,
            raw=raw,
            model=ModelSelection(id=model) if model else None,
            chat_uri=worker.resource,
        )
        if existing is not None:
            inner = existing.inner
        else:
            config = inner_config(spec, self.run_values(), opts)
            directories = (
                (wt.cwd.as_uri(),)
                if wt
                else ((folder.as_uri(),) if folder is not None else self.working_directories)
            )
            inner = await provider.create_session(
                AgentSessionContext(
                    session_uri=self.session_uri,
                    chat_uri=worker.resource,
                    provider_id=provider.agent.provider,
                    working_directories=directories,
                    model=model,
                    config=config,
                    publisher=ScopedPublisher(self.publisher, worker.resource),
                )
            )
            if chat_name is not None:
                self._named[chat_name] = _NamedChat(name, worker, inner, title, config, folder)
        self.workers[worker.resource] = inner
        pending = (
            self.pending_overlays.pop(worker.resource, None)
            if self.pending_overlays is not None
            else None
        )
        if pending:
            await forward_overlay(inner, worker.resource, pending)

        state = _Worker()
        usage = {"input_tokens": 0, "output_tokens": 0}
        value: Any = None

        async def on_waiting(waiting: bool) -> None:
            await self.sink.tool_call_output(
                call_id, [link_item], meta=flow.meta(flow.step(started, awaiting=waiting))
            )

        async def run(sink: TurnSink) -> None:
            nonlocal value
            state.started = True
            state.task = asyncio.current_task()
            tee = TurnTee(sink, redactor=self.redactor, on_waiting=on_waiting)
            try:
                if schema is None:
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
            except asyncio.CancelledError:
                if not state.aborting:
                    state.skipped = True
                    with contextlib.suppress(Exception):
                        await inner.cancel("skipped")
                raise
            except Exception as exc:
                state.error = self.redactor.mask(str(exc))
                raise
            finally:
                with contextlib.suppress(Exception):
                    await tee.flush()
                usage["input_tokens"] = tee.input_tokens
                usage["output_tokens"] = tee.output_tokens
                state.done.set()

        def turn_ended() -> None:
            # If `run` never started, the turn was cancelled before its first
            # step and saw no `CancelledError`: this is the only word that the
            # call is over, and it counts as a skip.
            if not state.started:
                state.skipped = True
            state.done.set()
            state.over.set()

        try:
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
            # The run was aborted (a worker skip cancels only the callback).
            state.aborting = True
            with contextlib.suppress(Exception):
                await inner.cancel("run aborted")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(state.done.wait(), ABORT_GRACE)
            if state.task is not None and not state.task.done():
                state.task.cancel()
            raise
        finally:
            self.workers.pop(worker.resource, None)
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
            raise FlowAgentError(state.error)
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
            return AgentResult(value, usage=usage)
        else:
            finished = flow.step_finished(
                status="ok", result=value, input_tokens=in_tok, output_tokens=out_tok
            )
        await self._complete(
            call_id, link, success=True, message="agent() returned", finished=finished
        )
        return AgentResult(value, usage=usage)


class _WorkerOut:
    """A remote call's output, written to its worker chat."""

    def __init__(self, sink: TurnSink, provider: str, chat: str, redactor: Redactor) -> None:
        self.sink = sink
        self.provider = provider
        self.chat = chat
        self.redactor = redactor

    async def linked(self, session_uri: str) -> None:
        await self.sink.text_delta(f"Remote session: {session_uri}\n")
        report_dispatch(DispatchHandle(provider=self.provider, chat=self.chat, session=session_uri))

    async def text(self, delta: str) -> None:
        await self.sink.text_delta(self.redactor.mask(delta))


class RemoteWorkerChats(StepDispatcher):
    """Shows each remote `agent()` call as a thin worker chat: the remote session and its text."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
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

        state = _Worker()
        usage = {"input_tokens": 0, "output_tokens": 0}
        value: Any = None

        async def run(sink: TurnSink) -> None:
            nonlocal value
            state.started = True
            state.task = asyncio.current_task()

            try:
                result = await body(_WorkerOut(sink, name, worker.resource, self.redactor), title)
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
            raise FlowAgentError(state.error)
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
    ) -> None:
        self.config = config
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
        name = route(self.config.routing, call)
        if name not in self.config.providers:
            configured = ", ".join(sorted(self.config.providers))
            raise FlowAgentError(
                f"provider {name!r} is not configured (opts.x.provider); configured: {configured}"
            )
        spec = self.config.providers[name]
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
        return await dispatcher.run_agent(call)

    async def aclose(self) -> None:
        for dispatcher in self._inner():
            close = getattr(dispatcher, "aclose", None)
            if close is not None:
                await close()
