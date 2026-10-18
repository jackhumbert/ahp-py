"""`FleetDispatcher`: each `agent()` call as a session on a gateway node (plan §2, §8 M7)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ahp_host import TurnSink
from ahp_host.provider.base import ClientToolCall, SessionPublisher
from ahp_protocol.ext import flow

from ahp_host_flow.config import APPROVAL_KEY, FlowConfig, ProviderConfig
from ahp_host_flow.dispatch import ABORT_GRACE, FlowAgentError, StepDispatcher, inner_config
from ahp_host_flow.engine import (
    AgentCall,
    AgentResult,
    DispatchHandle,
    Redactor,
    reattach_handle,
    report_dispatch,
    schema_error,
)

#: The gateway's flow tool names (ahp-gateway `FLOW_TOOLS`); duplicated because neither package
#: may import the other.
FLEET_TOOLS: Final = ("start_session", "wait_for_sessions", "read_session", "stop_session")
#: Seconds per `wait_for_sessions` call: a long node session is a loop of slices.
WAIT_SLICE: Final = 60.0
#: Seconds a fleet tool may take to answer (a `wait_for_sessions` slice comes on top). It
#: covers the gateway's default `slot_wait` (600 s), the longest a `start_session` waits for a
#: free slot under `max_running`; flow.toml's `reply_timeout` changes it per provider.
REPLY_TIMEOUT: Final = 900.0


def _offers_fleet(client: Mapping[str, Any]) -> bool:
    tools = client.get("tools")
    names = (
        {t.get("name") for t in tools if isinstance(t, Mapping)}
        if isinstance(tools, list)
        else set()
    )
    return all(tool in names for tool in FLEET_TOOLS)


class FleetClients:
    """Which active client of the flow session offers the fleet tools, as the host reports it.

    `generation` counts departures: it goes up whenever the offering client
    leaves or is replaced. A gateway that rejoins comes back under the same
    client id, so a call cannot tell from the id alone that the client it asked
    went away in between; it compares generations instead.
    """

    def __init__(self, clients: Sequence[Mapping[str, Any]] = ()) -> None:
        self._current: str | None = None
        self._changed = asyncio.Event()
        #: Set and replaced on every departure, so each waiter wakes once.
        self._departed = asyncio.Event()
        self.generation = 0
        self.update(clients)

    def update(self, clients: Sequence[Mapping[str, Any]]) -> None:
        before = self._current
        self._current = next(
            (
                str(c["clientId"])
                for c in clients
                if isinstance(c.get("clientId"), str) and _offers_fleet(c)
            ),
            None,
        )
        if before is not None and self._current != before:
            self.generation += 1
            departed, self._departed = self._departed, asyncio.Event()
            departed.set()
        if self._current is None:
            self._changed.clear()
        else:
            self._changed.set()

    def current(self) -> str | None:
        return self._current

    async def wait(self, timeout: float) -> str | None:
        """The offering client, waiting up to *timeout* seconds for one to join."""
        if self._current is None and timeout > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)
        return self._current

    async def departed_since(self, generation: int) -> None:
        """Return once the offering client has left since *generation*."""
        while self.generation == generation:
            await self._departed.wait()


class _StoppedError(Exception):
    """The run was aborted while a fleet call was in flight."""


@dataclass
class _FleetWorker:
    done: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    sink: TurnSink | None = None
    aborting: bool = False
    skipped: bool = False
    started: bool = False
    #: The node session has finished or a stop for it has been sent.
    settled: bool = False
    #: The host is stopping, not the run being aborted: the node session is left
    #: running, so the run resumed after the restart can reattach to it.
    detached: bool = False
    error: str | None = None


def _text_of(content: Any) -> str:
    blocks = content if isinstance(content, list) else []
    return " ".join(
        str(b["text"]) for b in blocks if isinstance(b, Mapping) and isinstance(b.get("text"), str)
    )


class FleetDispatcher(StepDispatcher):
    """Runs each `agent()` call on a gateway node through the gateway's fleet tools.

    The main turn's sink calls the tools directly (no model in between); the worker chat is a
    thin view: status, a link to the node session, the result and its usage.
    """

    def __init__(
        self,
        *,
        config: FlowConfig,
        sink: TurnSink,
        publisher: SessionPublisher,
        chat: str,
        clients: FleetClients,
        client_wait: float = 0.0,
        rejoin_wait: float = 10.0,
        run_values: Callable[[], Mapping[str, Any]] = lambda: {},
        run_model: str | None = None,
        aborted: Callable[[], bool] = lambda: True,
        redactor: Redactor | None = None,
        on_log: Callable[[str], None] | None = None,
        call_id_prefix: str = "agent",
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
        self.clients = clients
        #: Seconds a call waits for a gateway before its first fleet tool call: a continued
        #: run starts before the gateway has rejoined the restored session.
        self.client_wait = client_wait
        #: Seconds a fleet tool call whose gateway left waits for it to offer the tools
        #: again, to retry once.
        self.rejoin_wait = rejoin_wait
        #: The run's own model (the main chat's selection), for calls that name none.
        self.run_model = run_model
        #: Whether the run was aborted. A cancellation without an abort is the host
        #: stopping (or the session going): node sessions are then left running, for
        #: the run resumed after the restart to reattach to.
        self.aborted = aborted

    def _model(self, spec: ProviderConfig, opts: Mapping[str, Any]) -> str | None:
        """`opts.model` through the alias table, else the run's model where the provider has it.

        The run's model is the main chat's selection, from every provider's list. It is
        passed on when this provider's `models` table names it (as an alias or an id) or
        the provider has no table, so the node decides; else the node's default is used.
        """
        alias = opts.get("model")
        if isinstance(alias, str):
            return spec.models.get(alias, alias)
        run_model = self.run_model
        if run_model is None:
            return None
        if run_model in spec.models:
            return spec.models[run_model]
        if not spec.models or run_model in spec.models.values():
            return run_model
        return None

    async def _call(
        self,
        sink: TurnSink,
        call_id: str,
        name: str,
        args: Mapping[str, Any],
        suffix: str,
        *,
        reply_timeout: float,
        retry: bool,
    ) -> dict[str, Any]:
        """One fleet tool call, watched: retried once if its gateway left, else failed clearly.

        The call is lost when the client that offered the tools leaves (the host
        reports it gone) or no answer arrives within *reply_timeout* (a link that
        died without a word). A lost call is tried once more on the client the
        gateway rejoins with, if it does within `rejoin_wait`; `start_session` is
        never retried, since the first one may have started a session.
        """
        for attempt in range(2):
            client_id = self.clients.current()
            if client_id is None:
                client_id = await self.clients.wait(self.rejoin_wait)
            if client_id is None:
                raise FlowAgentError(
                    f"{name}: no gateway offers the fleet tools to this session "
                    f"(waited {self.rejoin_wait:g}s)"
                )
            generation = self.clients.generation
            bound = reply_timeout + (WAIT_SLICE if name == "wait_for_sessions" else 0.0)
            pending = asyncio.ensure_future(
                self._tool(
                    sink, client_id, call_id, name, args, suffix if attempt == 0 else f"{suffix}-r"
                )
            )
            departed = asyncio.ensure_future(self.clients.departed_since(generation))
            try:
                done, _ = await asyncio.wait(
                    {pending, departed}, timeout=bound, return_when=asyncio.FIRST_COMPLETED
                )
                if pending in done and (
                    pending.exception() is None or self.clients.generation == generation
                ):
                    return pending.result()
                lost = (
                    "the gateway offering the fleet tools left before answering"
                    if pending in done or departed in done
                    else f"no answer from the gateway within {bound:g}s"
                )
            finally:
                departed.cancel()
                if not pending.done():
                    pending.cancel()
            if not retry or attempt:
                raise FlowAgentError(f"{name}: {lost}")
            self._note(f"{name}: {lost}; trying once more when the gateway offers the tools again")
        raise AssertionError("unreachable")

    async def _tool(
        self,
        sink: TurnSink,
        client_id: str,
        call_id: str,
        name: str,
        args: Mapping[str, Any],
        suffix: str,
    ) -> dict[str, Any]:
        """Run one fleet tool through *sink* and return its structured result."""
        # A client's pump remembers (chat, toolCallId) for good, and the main chat outlives
        # turns whose ordinals restart at 0: without the run in the id, a later run's
        # first fleet call is taken for one already answered and never executes.
        unique = f"{self.run_id[:8]}-" if self.run_id else ""
        try:
            result = await sink.run_client_tool(
                ClientToolCall(
                    call_id=f"{unique}{call_id}-{suffix}",
                    name=name,
                    client_id=client_id,
                    tool_input=dict(args),
                    display_name=f"Fleet: {name}",
                )
            )
        except LookupError as exc:
            raise FlowAgentError(f"{name}: {exc}") from exc
        if not result.accepted:
            raise FlowAgentError(f"{name}: {result.reason or result.response}")
        value = result.value
        if isinstance(value, Mapping) and value.get("success") is False:
            raise FlowAgentError(f"{name}: {_text_of(value.get('content')) or 'the tool failed'}")
        data: Any = None
        if isinstance(value, Mapping):
            data = value.get("structuredContent")
            if not isinstance(data, Mapping):
                with contextlib.suppress(ValueError):
                    data = json.loads(_text_of(value.get("content")))
        if not isinstance(data, Mapping):
            raise FlowAgentError(f"{name}: the result is not an object")
        return dict(data)

    async def _run_call(self, call: AgentCall, name: str, title: str, call_id: str) -> AgentResult:
        spec: ProviderConfig = self.config.providers[name]
        client_id = self.clients.current()
        if client_id is None and self.client_wait > 0:
            client_id = await self.clients.wait(self.client_wait)
        if client_id is None:
            self._note(
                f"agent() #{call.ordinal}: no gateway offers fleet tools to this session; "
                f"provider {name!r} needs one"
            )
            raise FlowAgentError("no gateway offers fleet tools to this session")
        reply_timeout = spec.reply_timeout if spec.reply_timeout is not None else REPLY_TIMEOUT
        opts = call.opts
        worker = await self.publisher.open_tool_chat(title, tool_call_id=call_id, chat=self.chat)
        raw_schema = opts.get("schema")
        schema = raw_schema if isinstance(raw_schema, Mapping) else None
        started = self._steps[call_id]
        started["chat"] = worker.resource
        link_item = {"type": "subagent", "resource": worker.resource, "title": title}
        # Clients can open the worker chat while it runs.
        await self.sink.tool_call_output(call_id, [link_item], meta=flow.meta(flow.step(started)))

        # Read here: the worker callback runs in a task of its own.
        before = reattach_handle()
        old = (
            before.session
            if before is not None and before.provider == name and before.session
            else None
        )
        state = _FleetWorker()
        usage = {"input_tokens": 0, "output_tokens": 0}
        value: Any = None
        session: str | None = None

        async def call_tool(
            suffix: str, tool: str, args: Mapping[str, Any], *, starting: bool = False
        ) -> dict[str, Any]:
            """A main-sink fleet call, abandoned the moment the run is aborted."""
            nonlocal session
            if state.stop.is_set():
                raise _StoppedError
            pending = asyncio.ensure_future(
                self._call(
                    self.sink,
                    call_id,
                    tool,
                    args,
                    suffix,
                    reply_timeout=reply_timeout,
                    retry=not starting,
                )
            )
            stopper = asyncio.ensure_future(state.stop.wait())
            try:
                try:
                    await asyncio.wait({pending, stopper}, return_when=asyncio.FIRST_COMPLETED)
                    if not pending.done():
                        raise _StoppedError
                except (asyncio.CancelledError, _StoppedError):
                    if starting and not pending.done():
                        # The node may already have started a session this run has not heard
                        # of yet: give the answer a moment, so the session can be stopped.
                        await asyncio.wait({pending}, timeout=ABORT_GRACE)
                    if starting and pending.done() and not pending.cancelled():
                        early = pending.exception()
                        found = early is None and pending.result().get("session")
                        if isinstance(found, str) and found:
                            session = found
                    raise
                if pending.cancelled():
                    raise (
                        _StoppedError
                        if state.stop.is_set()
                        else FlowAgentError(f"{tool}: cancelled")
                    )
                try:
                    return pending.result()
                except FlowAgentError:
                    if state.stop.is_set():
                        raise _StoppedError from None
                    raise
            finally:
                stopper.cancel()
                if not pending.done():
                    pending.cancel()

        async def stop_node(sink: TurnSink) -> None:
            if session is None or state.detached:
                return
            state.settled = True
            with contextlib.suppress(Exception):
                await self._call(
                    sink,
                    call_id,
                    "stop_session",
                    {"session": session},
                    "stop",
                    reply_timeout=ABORT_GRACE,
                    retry=False,
                )

        async def run(worker_sink: TurnSink) -> None:
            nonlocal value, session
            state.started = True
            state.task = asyncio.current_task()
            state.sink = worker_sink
            try:
                if old is not None:
                    await worker_sink.text_delta(f"Reattaching to {old}\n")
                    try:
                        probe = await call_tool(
                            "reattach", "wait_for_sessions", {"sessions": [old], "timeout": 1.0}
                        )
                        rows = probe.get("sessions")
                        row = rows[0] if isinstance(rows, list) and rows else None
                        gone = isinstance(row, Mapping) and row.get("status") == "gone"
                    except FlowAgentError:
                        gone = True
                    if gone:
                        self._note(
                            f"agent() #{call.ordinal}: node session {old} is gone; "
                            "dispatching it again"
                        )
                    else:
                        self._note(f"agent() #{call.ordinal}: reattached to node session {old}")
                        session = old
                        report_dispatch(
                            DispatchHandle(provider=name, chat=worker.resource, session=session)
                        )
                if session is None:
                    await worker_sink.text_delta(f"Starting on {spec.node} ({spec.agent})\n")
                    args: dict[str, Any] = {
                        "node": spec.node,
                        "agent": spec.agent,
                        "prompt": call.prompt,
                        "title": title,
                    }
                    model = self._model(spec, opts)
                    if model is not None:
                        args["model"] = model
                    config = inner_config(spec, self.run_values(), opts)
                    if config:
                        args["config"] = config
                    if schema is not None:
                        args["output_schema"] = dict(schema)
                    try:
                        started_data = await call_tool(
                            "start", "start_session", args, starting=True
                        )
                    except FlowAgentError as exc:
                        mode = config.get(APPROVAL_KEY)
                        if mode is None or APPROVAL_KEY not in str(exc):
                            raise
                        # Said in so many words: the run asked for an approval mode the
                        # gateway's `child_config` does not allow, and only a config change
                        # on one side or the other gets the call through.
                        refused = (
                            f"the gateway refused approval mode {mode!r} for provider "
                            f"{name!r} ({exc}); choose a stricter one with approval_mode in "
                            "flow.toml or the run's Approvals setting, or loosen the gateway's "
                            "child_config"
                        )
                        self._note(f"agent() #{call.ordinal}: {refused}")
                        raise FlowAgentError(refused) from None
                    node_session = started_data.get("session")
                    if not isinstance(node_session, str) or not node_session:
                        raise FlowAgentError("start_session: no session in the result")
                    session = node_session
                    report_dispatch(
                        DispatchHandle(provider=name, chat=worker.resource, session=session)
                    )
                    await worker_sink.text_delta(f"Node session: {session}\n")
                i = 0
                while True:
                    waited = await call_tool(
                        f"wait-{i}",
                        "wait_for_sessions",
                        {"sessions": [session], "timeout": WAIT_SLICE},
                    )
                    i += 1
                    entries = waited.get("sessions")
                    first = entries[0] if isinstance(entries, list) and entries else None
                    if not isinstance(first, Mapping):
                        raise FlowAgentError("wait_for_sessions: no session in the result")
                    if first.get("done") is True:
                        break
                data = await call_tool("read", "read_session", {"session": session})
                state.settled = True
                status = data.get("status")
                if status == "done":
                    value = data.get("structured") if schema is not None else data.get("text")
                elif status != "stopped":
                    reason = data.get("error")
                    state.error = self.redactor.mask(
                        reason if isinstance(reason, str) and reason else f"node session {status}"
                    )
                if value is not None:
                    shown = (
                        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    )
                    await worker_sink.text_delta(self.redactor.mask(shown))
                raw = data.get("usage")
                if isinstance(raw, Mapping):
                    for key in usage:
                        count = raw.get(key)
                        usage[key] = count if isinstance(count, int) else 0
                await worker_sink.usage(
                    input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]
                )
            except _StoppedError:
                await stop_node(worker_sink)
            except asyncio.CancelledError:
                # A skip (the worker turn cancelled), unless the run is going down. The
                # node session is stopped by the call, not here: when the host stops, it
                # cancels every turn, and this one can be reached before the run's own,
                # which is what says the node session must be left running.
                if not state.aborting:
                    state.skipped = True
                raise
            except Exception as exc:
                state.error = self.redactor.mask(str(exc))
                raise
            finally:
                state.done.set()

        def turn_ended() -> None:
            # If `run` never started, the turn was cancelled before its first step and saw no
            # `CancelledError`: this is the only word that the call is over, a skip.
            if not state.started:
                state.skipped = True
            state.done.set()

        try:
            if not await worker.run_turn(call.prompt, run, on_end=turn_ended):
                raise FlowAgentError("could not start a turn in the worker chat")
            await state.done.wait()
            if state.skipped and not state.settled:
                await stop_node(self.sink)
        except asyncio.CancelledError:
            # The run is going down (a worker skip cancels only the callback): aborted, or
            # the host stopping. Only an abort stops the node session; the host stopping
            # leaves it running, so the run resumed after the restart reattaches to it.
            state.aborting = True
            state.detached = not self.aborted()
            state.stop.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(state.done.wait(), ABORT_GRACE)
            if state.task is not None and not state.task.done():
                state.task.cancel()
            if not state.settled and state.sink is not None:
                # The callback died before it could stop its node session.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(stop_node(state.sink), ABORT_GRACE)
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
        value = self.redactor.mask_value(value)
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
