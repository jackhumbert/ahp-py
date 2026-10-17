"""`ProviderDispatcher`: each `agent()` call as a worker chat on an inner provider (plan §4.4)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from ahp_host import TurnSink
from ahp_host.core.structured import StructuredTurn
from ahp_host.core.turn import ActionTurnSink
from ahp_host.provider.base import (
    AgentProvider,
    AgentSession,
    AgentSessionContext,
    ModelSelection,
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
    DispatchHandle,
    Redactor,
    report_dispatch,
    schema_error,
)
from ahp_host_flow.routing import route
from ahp_host_flow.worktree import (
    Worktree,
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


class ProviderDispatcher:
    """Runs each `agent()` call as a turn of an inner provider session in its own worker chat."""

    reports_dispatch: bool = True

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
    ) -> None:
        self.config = config
        self.providers = providers
        self.sink = sink
        self.publisher = publisher
        self.chat = chat
        self.session_uri = session_uri
        self.working_directories = tuple(working_directories)
        self.run_values = run_values
        self.run_model = run_model
        self.workers: MutableMapping[str, AgentSession] = workers if workers is not None else {}
        #: Overlays that arrived for a worker chat before its inner session existed.
        self.pending_overlays = pending_overlays
        self.redactor = redactor if redactor is not None else make_redactor(config.redact)
        self.on_log = on_log
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
        call_id = f"agent-{call.ordinal}"
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

    async def _run_call(self, call: AgentCall, name: str, title: str, call_id: str) -> AgentResult:
        try:
            provider = self.providers[name]
        except ConfigError as exc:
            _log.warning("agent() #%s: provider %r is unavailable: %s", call.ordinal, name, exc)
            raise FlowAgentError(f"provider {name!r} is unavailable: {exc}") from exc
        opts = call.opts
        effort = opts.get("effort")
        if effort is not None and not (isinstance(effort, str) and effort in EFFORT_LEVELS):
            self._note(
                f"agent() #{call.ordinal}: effort {effort!r} is not one of "
                f"{', '.join(EFFORT_LEVELS)}; ignored"
            )
        wt: Worktree | None = None
        if opts.get("isolation") == "worktree":
            workdir = next(
                (d for w in self.working_directories if (d := catalog.directory_of(w)) is not None),
                None,
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
        try:
            return await self._run_worker(call, name, title, call_id, provider, wt)
        finally:
            # Every exit after the worktree exists (a failed or cancelled setup too) releases it.
            if wt is not None:
                await self._release_through_cancel(wt, call)

    async def _release_through_cancel(self, wt: Worktree, call: AgentCall) -> None:
        """Release *wt* to the end even if this task is cancelled again meanwhile."""
        releasing = asyncio.ensure_future(self._release(wt, call))
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

    async def _release(self, wt: Worktree, call: AgentCall) -> None:
        try:
            if await release_worktree(wt):
                self._note(f"agent() #{call.ordinal}: worktree kept at {wt.path} (it has changes)")
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
    ) -> AgentResult:
        spec = self.config.providers[name]
        opts = call.opts
        worker = await self.publisher.open_tool_chat(title, tool_call_id=call_id, chat=self.chat)
        report_dispatch(DispatchHandle(provider=name, chat=worker.resource))

        raw_schema = opts.get("schema")
        schema = raw_schema if isinstance(raw_schema, Mapping) else None
        raw: dict[str, Any] = {"text": call.prompt, "origin": {"kind": "agent"}}
        if schema:
            raw["_meta"] = {OUTPUT_SCHEMA_META_KEY: dict(schema)}
        model = model_for(spec, provider, opts, self.run_model)
        started = self._steps[call_id]
        started["chat"] = worker.resource
        started["model"] = model
        link_item = {"type": "subagent", "resource": worker.resource, "title": title}
        # Clients can open the child chat while it runs.
        await self.sink.tool_call_output(call_id, [link_item], meta=flow.meta(flow.step(started)))
        message = UserMessage(
            text=call.prompt,
            raw=raw,
            model=ModelSelection(id=model) if model else None,
            chat_uri=worker.resource,
        )
        inner = await provider.create_session(
            AgentSessionContext(
                session_uri=self.session_uri,
                chat_uri=worker.resource,
                provider_id=provider.agent.provider,
                working_directories=(wt.cwd.as_uri(),) if wt else self.working_directories,
                model=model,
                config=inner_config(spec, self.run_values(), opts),
                publisher=ScopedPublisher(self.publisher, worker.resource),
            )
        )
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

        try:
            # `on_end` is the only word of a turn cancelled before `run` starts.
            if not await worker.run_turn(call.prompt, run, on_end=turn_ended):
                raise FlowAgentError("could not start a turn in the worker chat")
            # `run_turn` returns when the turn starts, so wait on our own signal.
            await state.done.wait()
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
            if self.pending_overlays is not None:
                self.pending_overlays.pop(worker.resource, None)
            if state.skipped and not state.started:
                with contextlib.suppress(Exception):
                    await inner.cancel("skipped")
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
