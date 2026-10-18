"""Library API: run agents from Python with no script and no runner process (plan §4.10)."""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from typing import Any

from ahp_host_flow.engine import (
    LIBRARY_FILENAME,
    MAX_ITEMS_PER_CALL,
    AbortSignal,
    AgentCall,
    Dispatcher,
    GateRunner,
    JournalRun,
    Registry,
    RunOptions,
    Scheduler,
    SchedulerRefusal,
)
from ahp_host_flow.sandbox import pyrunner
from ahp_host_flow.sandbox.client import (
    NO_ARGS,
    RunState,
    SandboxError,
    resolve_for_run,
    run_workflow_child,
)


class _Budget:
    """`total`, `spent()` and `remaining()` as scripts see them, read from the scheduler."""

    def __init__(self, scheduler: Scheduler, total: int | None) -> None:
        self._scheduler = scheduler
        self.total = total

    def spent(self) -> int:
        return self._scheduler.budget.spent

    def remaining(self) -> float:
        if self.total is None:
            return math.inf
        return max(0, self.total - self.spent())


class FlowRun:
    """The verbs of one in-process run; get one from `run()`."""

    def __init__(self, state: RunState, run_id: str | None) -> None:
        self._state = state
        self._scheduler = state.scheduler
        self._tasks: set[asyncio.Future[Any]] = set()
        self.run_id = run_id
        self.budget = _Budget(state.scheduler, state.options.budget_total)

    def _track(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def agent(self, prompt: str, **opts: Any) -> asyncio.Future[Any]:
        if not isinstance(prompt, str):
            return pyrunner.failed(TypeError("agent() needs a prompt string"))
        try:
            opts = pyrunner.clone_json(opts)
        except (TypeError, ValueError) as exc:
            return pyrunner.failed(TypeError(f"agent() opts must be JSON: {exc}"))
        phase, label = opts.get("phase"), opts.get("label")
        try:
            call, cached = self._scheduler.issue(
                prompt,
                opts,
                phase=phase if isinstance(phase, str) else None,
                label=label if isinstance(label, str) else None,
            )
        except SchedulerRefusal as exc:
            return pyrunner.failed(exc)
        return self._track(self._scheduler.run(call, cached))

    def parallel(self, thunks: object) -> asyncio.Future[list[Any]]:
        return pyrunner.run_parallel(thunks, MAX_ITEMS_PER_CALL)

    def pipeline(self, items: object, *stages: object) -> asyncio.Future[list[Any]]:
        return pyrunner.run_pipeline(items, stages, MAX_ITEMS_PER_CALL)

    def phase(self, title: str) -> None:
        text = str(title)
        self._scheduler.set_phase(text)
        if self._state.on_phase is not None:
            self._state.on_phase(text)

    def log(self, message: str) -> None:
        if self._state.on_log is not None:
            self._state.on_log(str(message))

    def workflow(
        self, name_or_ref: str | Mapping[str, str], args: Any = pyrunner.MISSING
    ) -> asyncio.Future[Any]:
        ref: str | dict[str, str]
        if isinstance(name_or_ref, str):
            ref = name_or_ref
        elif isinstance(name_or_ref, Mapping) and isinstance(name_or_ref.get("scriptPath"), str):
            ref = {"scriptPath": name_or_ref["scriptPath"]}
        else:
            return pyrunner.failed(TypeError("workflow() needs a name or {scriptPath}"))
        if args is not pyrunner.MISSING:
            try:
                args = pyrunner.clone_json(args)
            except (TypeError, ValueError) as exc:
                return pyrunner.failed(TypeError(f"workflow() args must be JSON: {exc}"))
        return self._track(self._workflow(ref, args))

    async def _workflow(self, ref: object, args: Any) -> Any:
        resolved = await resolve_for_run(self._state, ref)
        # The child always gets a runner of its own, so it cannot nest.
        return await run_workflow_child(
            self._state, resolved, NO_ARGS if args is pyrunner.MISSING else args
        )

    async def _cancel_pending(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@contextlib.asynccontextmanager
async def run(
    dispatcher: Dispatcher,
    options: RunOptions | None = None,
    *,
    abort: AbortSignal | None = None,
    node: str | None = None,
    on_log: Callable[[str], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    on_agent_start: Callable[[AgentCall], None] | None = None,
    on_agent_end: Callable[[AgentCall, Any], None] | None = None,
    on_run_start: Callable[[str], None] | None = None,
) -> AsyncIterator[FlowRun]:
    """Run agents in-process on the same Scheduler and journal as a script run.

    Calls the body does not await are cancelled when it exits. With
    `options.state_dir` the run is journaled with no script snapshot (filename
    `<library>`) and `options.resume_from` replays by key.
    """
    options = options or RunOptions()
    jr: JournalRun | None = None
    if options.state_dir is not None:
        jr = JournalRun.open(
            options,
            source=None,
            filename=LIBRARY_FILENAME,
            has_args=False,
            args=None,
            on_log=on_log,
            scriptless=True,
        )
        options = jr.options
    try:
        scheduler = Scheduler(
            dispatcher,
            options,
            abort=abort,
            on_log=on_log,
            on_agent_start=on_agent_start,
            on_agent_end=on_agent_end,
            journal=jr.writer if jr else None,
            replay=jr.replay if jr else None,
        )
    except BaseException:
        if jr is not None:
            jr.close()
        raise
    # No gate answerer in-process: ask()/approve() fail clearly (like headless flow run).
    gate_runner = GateRunner(None, abort=abort, mask=options.redact, on_log=on_log)
    state = RunState(
        scheduler=scheduler,
        options=options,
        registry=Registry(options.workflow_dirs, python_dirs=options.python_dirs),
        journal=jr,
        node=node,
        on_log=on_log,
        on_phase=on_phase,
        gates=gate_runner,
    )
    flow = FlowRun(state, jr.run_id if jr else None)
    try:
        if jr is not None and on_run_start is not None:
            on_run_start(jr.run_id)
        yield flow
    finally:
        await flow._cancel_pending()
        state.kill_children(SandboxError("run ended"))
        gate_runner.close()
        scheduler.close()
        if jr is not None:
            jr.close()
