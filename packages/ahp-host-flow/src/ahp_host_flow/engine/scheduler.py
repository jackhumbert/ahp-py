"""Scheduler: the concurrency cap, per-run limits and abort around a dispatcher."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ahp_host_flow.engine.abort import AbortSignal
from ahp_host_flow.engine.budget import OUTPUT_TOKENS, Budget
from ahp_host_flow.engine.dispatcher import (
    _DISPATCH_REPORT,
    _REATTACH,
    AgentCall,
    AgentResult,
    Dispatcher,
    DispatchHandle,
    ReportsDispatch,
)
from ahp_host_flow.engine.journal import (
    AgentRecord,
    JournalStatus,
    JournalWriter,
    StartRecord,
    call_key,
    canonical_json,
)
from ahp_host_flow.engine.options import RunOptions
from ahp_host_flow.engine.schema import schema_error

if TYPE_CHECKING:
    from ahp_host_flow.engine.resume import Replay


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
        journal: JournalWriter | None = None,
        replay: Replay | None = None,
    ) -> None:
        options = options if options is not None else RunOptions()
        self._dispatcher = dispatcher
        self._concurrency = options.resolved_concurrency()
        self._slots = asyncio.Semaphore(self._concurrency)
        self._budget = Budget(options.budget_total)
        self._max_agents = options.max_agents
        self._next_ordinal = 0
        self._phase: str | None = None
        self._inflight: set[asyncio.Task[AgentResult]] = set()
        self._aborted = False
        self._on_log = on_log
        self._on_agent_start = on_agent_start
        self._on_agent_end = on_agent_end
        self._journal = journal
        self._replay = replay
        self._reattach: dict[int, DispatchHandle] = {}
        self._key_extra = options.key_extra
        # Keys taken at issue time, by ordinal, until the call is dispatched or served.
        self._keys: dict[int, str] = {}
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

    @property
    def phase(self) -> str | None:
        return self._phase

    def set_phase(self, title: str | None) -> None:
        self._phase = title

    @property
    def issued(self) -> int:
        return self._next_ordinal

    def issue(
        self,
        prompt: str,
        opts: Mapping[str, Any],
        *,
        phase: str | None = None,
        label: str | None = None,
    ) -> tuple[AgentCall, AgentRecord | None]:
        """Take the next ordinal and admit the call; sync, so ordinals, phases and refusals
        follow issue order. phase None means the run's current phase. A refused call takes
        no ordinal.
        """
        if self._next_ordinal >= self._max_agents:
            raise AgentLimitError(
                f"agent() limit reached: at most {self._max_agents} agent() calls per run"
            )
        call = AgentCall(
            ordinal=self._next_ordinal,
            prompt=prompt,
            opts=dict(opts),
            phase=phase if phase is not None else self._phase,
            label=label,
        )
        # Before dispatch and before the ordinal is taken: a failing hook takes no ordinal.
        key = self._key(call)
        cached = self.admit(call, key)
        self._keys[call.ordinal] = key
        self._next_ordinal += 1
        return call, cached

    def _key(self, call: AgentCall) -> str:
        if self._key_extra is None:
            return call_key(call)
        try:
            extra = self._key_extra(call)
            canonical_json(extra)
        except Exception as exc:
            raise ValueError(f"resume key hook failed for agent() #{call.ordinal}: {exc}") from exc
        return call_key(call, extra=extra)

    def _log(self, line: str) -> None:
        if self._on_log is not None:
            self._on_log(line)

    def _check_budget(self) -> None:
        if self._budget.exhausted:
            spent, total = self._budget.spent, self._budget.total
            raise BudgetExhaustedError(f"budget exhausted: {spent} of {total} output tokens spent")

    def admit(self, call: AgentCall, key: str | None = None) -> AgentRecord | None:
        """Refuse a call past a hard cap; sync, so checks happen in issue order.

        Returns the journal record that serves the call on resume, else None. *key* is the
        call's key when the caller already took it, else it is computed here.
        """
        # Ordinals are assigned at issue time, so this cap is deterministic (plan §9).
        if call.ordinal >= self._max_agents:
            raise AgentLimitError(
                f"agent() limit reached: at most {self._max_agents} agent() calls per run"
            )
        if self._replay is not None:
            if key is None:
                key = self._key(call)
            rec = self._replay.lookup(call, key)
            if rec is not None:
                # The record proves the original run admitted it; no budget re-check.
                return rec
            handle = self._replay.reattach(call, key)
            if handle is not None:
                self._reattach[call.ordinal] = handle
        self._check_budget()
        return None

    async def run(self, call: AgentCall, cached: AgentRecord | None = None) -> Any:
        """Dispatch one call under the cap; the JSON value for agent(), None for null."""
        n = call.ordinal
        if cached is not None:
            self._keys.pop(n, None)
            if self._aborted:
                self._log(f"agent() #{n} aborted")
                return None
            self._budget.add({OUTPUT_TOKENS: cached.output_tokens})
            if self._journal is not None:
                self._journal.append_agent(cached)
            return cached.value if cached.status == "ok" else None
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
        key = self._keys.pop(n, None) or self._key(call)
        reported = False

        def report(handle: DispatchHandle | None) -> None:
            nonlocal reported
            if reported:
                return
            reported = True
            if self._journal is not None:
                h = handle if handle is not None else DispatchHandle()
                self._journal.append_started(StartRecord(n, key, h.provider, h.chat, h.session))

        reports = (
            isinstance(self._dispatcher, ReportsDispatch) and self._dispatcher.reports_dispatch
        )
        if not reports:
            report(None)
        # Set around create_task only, so just the task's copied context sees it.
        handle = self._reattach.pop(n, None)
        token = _DISPATCH_REPORT.set(report)
        reattach_token = _REATTACH.set(handle)
        try:
            task = asyncio.create_task(self._dispatcher.run_agent(call))
        finally:
            _REATTACH.reset(reattach_token)
            _DISPATCH_REPORT.reset(token)
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
            # A start record always precedes the agent record.
            report(None)
        if task.cancelled():
            # A dispatcher that cancels its own turn is a skip (plan §4.4).
            self._log(f"agent() #{n} {'aborted' if self._aborted else 'skipped'}")
            # An aborted call did not complete; resume reruns it.
            if not self._aborted:
                self._record(AgentRecord(n, key, "null", None, 0))
            return None
        exc = task.exception()
        if exc is not None:
            self._log(f"agent() #{n} failed: {exc}")
            self._record(AgentRecord(n, key, "error", None, 0, error=str(exc)))
            return None
        result = task.result()
        added = self._budget.add(result.usage)
        try:
            json.dumps(result.value, allow_nan=False)
        except (TypeError, ValueError) as err:
            self._log(f"agent() #{n} failed: result is not JSON: {err}")
            self._record(
                AgentRecord(n, key, "error", None, added, error=f"result is not JSON: {err}")
            )
            return None
        schema = call.opts.get("schema")
        if isinstance(schema, Mapping) and result.value is not None:
            error = schema_error(schema, result.value)
            if error is not None:
                self._log(f"agent() #{n} failed: {error}")
                self._record(AgentRecord(n, key, "error", None, added, error=error))
                return None
        status: JournalStatus = "null" if result.value is None else "ok"
        self._record(AgentRecord(n, key, status, result.value, added))
        return result.value

    def _record(self, record: AgentRecord) -> None:
        if self._journal is not None:
            self._journal.append_agent(record)

    def abort(self) -> None:
        """Cancel every in-flight dispatch; later calls resolve null. Idempotent."""
        self._aborted = True
        for task in list(self._inflight):
            task.cancel()

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
