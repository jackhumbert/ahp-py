"""Scheduler: the concurrency cap, per-run limits and abort around a dispatcher."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from ahp_host_flow.engine.abort import AbortSignal
from ahp_host_flow.engine.budget import Budget
from ahp_host_flow.engine.dispatcher import AgentCall, AgentResult, Dispatcher
from ahp_host_flow.engine.options import RunOptions


class SchedulerRefusal(Exception):  # noqa: N818
    """agent() must throw in the script, not return null."""


class AgentLimitError(SchedulerRefusal):
    pass


class BudgetExhaustedError(SchedulerRefusal):
    pass


class Scheduler:
    def __init__(
        self,
        dispatcher: Dispatcher,
        options: RunOptions | None = None,
        *,
        abort: AbortSignal | None = None,
        on_log: Callable[[str], None] | None = None,
        on_agent_start: Callable[[AgentCall], None] | None = None,
        on_agent_end: Callable[[AgentCall, Any], None] | None = None,
    ) -> None:
        options = options if options is not None else RunOptions()
        self._dispatcher = dispatcher
        self._concurrency = options.resolved_concurrency()
        self._slots = asyncio.Semaphore(self._concurrency)
        self._budget = Budget(options.budget_total)
        self._max_agents = options.max_agents
        self._inflight: set[asyncio.Task[AgentResult]] = set()
        self._aborted = False
        self._on_log = on_log
        self._on_agent_start = on_agent_start
        self._on_agent_end = on_agent_end
        self._unsubscribe: Callable[[], None] | None = None
        if abort is not None:
            self._unsubscribe = abort.subscribe(self.abort)

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def concurrency(self) -> int:
        return self._concurrency

    def _log(self, line: str) -> None:
        if self._on_log is not None:
            self._on_log(line)

    def _check_budget(self) -> None:
        if self._budget.exhausted:
            spent, total = self._budget.spent, self._budget.total
            raise BudgetExhaustedError(f"budget exhausted: {spent} of {total} output tokens spent")

    def admit(self, call: AgentCall) -> None:
        """Refuse a call past a hard cap; sync, so checks happen in issue order."""
        # Ordinals are assigned in JS at issue time, so this cap is deterministic (plan §9).
        if call.ordinal >= self._max_agents:
            raise AgentLimitError(
                f"agent() limit reached: at most {self._max_agents} agent() calls per run"
            )
        self._check_budget()

    async def run(self, call: AgentCall) -> Any:
        """Dispatch one call under the cap; the JSON value for agent(), None for null."""
        n = call.ordinal
        if self._aborted:
            self._log(f"agent() #{n} aborted")
            return None
        async with self._slots:
            if self._aborted:
                self._log(f"agent() #{n} aborted")
                return None
            # Hard ceiling also for calls that queued behind the cap.
            self._check_budget()
            value: Any = None
            if self._on_agent_start is not None:
                self._on_agent_start(call)
            try:
                value = await self._dispatch(call)
            finally:
                if self._on_agent_end is not None:
                    self._on_agent_end(call, value)
            return value

    async def _dispatch(self, call: AgentCall) -> Any:
        n = call.ordinal
        task = asyncio.create_task(self._dispatcher.run_agent(call))
        self._inflight.add(task)
        try:
            # wait() never raises the inner task's cancellation; an outer one still
            # raises here, and the finally cancels the dispatch.
            await asyncio.wait({task})
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._inflight.discard(task)
        if task.cancelled():
            # A dispatcher that cancels its own turn is a skip (plan §4.4).
            self._log(f"agent() #{n} {'aborted' if self._aborted else 'skipped'}")
            return None
        exc = task.exception()
        if exc is not None:
            self._log(f"agent() #{n} failed: {exc}")
            return None
        result = task.result()
        self._budget.add(result.usage)
        try:
            json.dumps(result.value, allow_nan=False)
        except (TypeError, ValueError) as err:
            self._log(f"agent() #{n} failed: result is not JSON: {err}")
            return None
        return result.value

    def abort(self) -> None:
        """Cancel every in-flight dispatch; later calls resolve null. Idempotent."""
        self._aborted = True
        for task in list(self._inflight):
            task.cancel()

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
