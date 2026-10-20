"""Scheduler: the concurrency cap, per-run limits and abort around a dispatcher."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ahp_host_flow.engine.abort import AbortSignal
from ahp_host_flow.engine.budget import (
    CACHE_READ_TOKENS,
    COST_USD,
    COUNTERS,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    WALL_SECONDS,
    Budget,
)
from ahp_host_flow.engine.dispatcher import (
    _DISPATCH_REPORT,
    _REATTACH,
    _STOP_CAUSE,
    _USAGE_ATTEMPT,
    _USAGE_REPORT,
    AgentCall,
    AgentResult,
    Dispatcher,
    DispatchError,
    DispatchHandle,
    ReportsDispatch,
    StopCause,
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


def _whole(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _money(value: Any) -> float:
    ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    return float(value) if ok and value > 0 else 0.0


_TOKEN_KEYS = (OUTPUT_TOKENS, INPUT_TOKENS, CACHE_READ_TOKENS)


def _sum_usage(parts: list[Mapping[str, Any]]) -> dict[str, Any]:
    """The per-attempt usages of one call, summed (token counts and cost)."""
    total: dict[str, Any] = {key: sum(_whole(p.get(key)) for p in parts) for key in _TOKEN_KEYS}
    cost = sum(_money(p.get(COST_USD)) for p in parts)
    if cost:
        total[COST_USD] = cost
    return total


def _settled_usage(
    result: Mapping[str, Any], cost: float | None, parts: Mapping[str, Any] | None
) -> tuple[dict[str, Any], float | None]:
    """A finished call's usage: per counter the larger of its result and its summed attempts.

    A retried call's result already sums its attempts; a fallback's result is only the last
    provider's, and the slots add the failed attempts before it.
    """
    usage: dict[str, Any] = dict(result)
    if parts is None:
        return usage, cost
    for key in _TOKEN_KEYS:
        if _whole(parts.get(key)) > _whole(usage.get(key)):
            usage[key] = _whole(parts.get(key))
    summed = _money(parts.get(COST_USD))
    if summed > _money(cost):
        cost = summed
    return usage, cost


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
        self._budget = Budget(
            options.budget_total,
            input_total=options.input_budget,
            cost_total=options.cost_budget,
            wall_total=options.wall_budget,
            count_cache=options.count_cache,
        )
        self._wall = options.wall_budget is not None
        self._wall_total = options.wall_budget
        # Taken at first use inside the loop, and only with a wall budget: a Scheduler without
        # one can be built outside a running loop.
        self._t0: float | None = None
        self._count_cache = options.count_cache
        self._reserve = options.reserve
        self._quota = options.quota
        # Output tokens reserved at admission by calls not yet over, by ordinal.
        self._reserved: dict[int, int] = {}
        self._finished_out = 0
        self._finished_n = 0
        # What a running call reported so far, every attempt summed, by ordinal.
        self._live: dict[int, Mapping[str, Any]] = {}
        self._tasks: dict[int, asyncio.Task[AgentResult]] = {}
        # Each running call's stop cause, set before the hard cap cancels it.
        self._causes: dict[int, StopCause] = {}
        # Calls the hard cap stopped, with the reason each was told.
        self._budget_cancelled: set[int] = set()
        self._stop_reason: dict[int, str] = {}
        self._stopped: set[str] = set()
        self._wall_timer: asyncio.TimerHandle | None = None
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

    def _elapsed(self) -> float | None:
        """Seconds since the wall clock started (its first use starts it and its timer).

        None without a wall budget, or outside a running loop.
        """
        if not self._wall:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._t0 is None:
            t0 = self._t0 = loop.time()
            wall = self._wall_total
            assert wall is not None
            self._wall_timer = loop.call_later(
                wall,
                lambda: self._live_stop(
                    self._message(WALL_SECONDS, max(loop.time() - t0, wall), wall)
                ),
            )
        return loop.time() - self._t0

    def _check_budget(self, ordinal: int | None = None) -> None:
        elapsed = self._elapsed()
        if elapsed is not None:
            self._budget.set_wall(elapsed)
        message = self._budget.refusal()
        total = self._budget.total
        if message is None and total is not None:
            # What other calls reserved counts as spent; this call's own reservation does not.
            others = sum(r for n, r in self._reserved.items() if n != ordinal)
            if self._budget.spent + others >= total:
                message = self._message(OUTPUT_TOKENS, self._budget.spent, total)
        if message is not None:
            raise BudgetExhaustedError(message)

    def _check_quota(self, ordinal: int) -> None:
        if self._quota is None:
            return
        text = self._quota.check()
        if text is not None:
            self._log(f"{text}; agent() #{ordinal} refused")
            raise BudgetExhaustedError(text)

    def _uncharged(self) -> tuple[int, float]:
        """Tokens and cost the running calls have reported and the quota has not been charged."""
        tokens = 0
        cost = 0.0
        for usage in self._live.values():
            tokens += self._tokens_of(usage)
            cost += _money(usage.get(COST_USD))
        return tokens, cost

    def _tokens_of(self, usage: Mapping[str, Any]) -> int:
        tokens = _whole(usage.get(OUTPUT_TOKENS)) + _whole(usage.get(INPUT_TOKENS))
        if self._count_cache:
            tokens += _whole(usage.get(CACHE_READ_TOKENS))
        return tokens

    @staticmethod
    def _message(name: str, value: float, limit: float) -> str:
        """The refusal text of counter *name*, worded as `Budget.refusal` words it."""
        if name == OUTPUT_TOKENS:
            return f"budget exhausted: {int(value)} of {int(limit)} output tokens spent"
        if name == INPUT_TOKENS:
            return f"input budget exhausted: {int(value)} of {int(limit)} input tokens spent"
        if name == COST_USD:
            return f"cost budget exhausted: ${value:.4f} of ${limit:.4f} spent"
        return f"wall budget exhausted: {value:.1f} of {limit:g} s elapsed"

    def _effective(self, name: str) -> float:
        """Counter *name*: what calls settled plus what running calls have reported."""
        value = self._budget.value(name)
        for usage in self._live.values():
            if name == OUTPUT_TOKENS:
                value += _whole(usage.get(OUTPUT_TOKENS))
            elif name == INPUT_TOKENS:
                value += _whole(usage.get(INPUT_TOKENS))
                if self._count_cache:
                    value += _whole(usage.get(CACHE_READ_TOKENS))
            elif name == COST_USD:
                value += _money(usage.get(COST_USD))
        return value

    def _check_live(self) -> None:
        """Stop the running calls once a counter, live usage included, is past its limit."""
        elapsed = self._elapsed()
        if elapsed is not None:
            self._budget.set_wall(elapsed)
        for name in COUNTERS:
            limit = self._budget.limit(name)
            if limit is None:
                continue
            value = self._effective(name)
            if value >= limit if name == WALL_SECONDS else value > limit:
                self._live_stop(self._message(name, value, limit))
                return
        if self._quota is not None:
            tokens, cost = self._uncharged()
            text = self._quota.check(extra_tokens=tokens, extra_cost=cost, live=True)
            if text is not None:
                self._live_stop(text)

    def _live_stop(self, reason: str) -> None:
        """Cancel every call in flight, once per reason; each ends as a budget error.

        With nothing in flight it does nothing: later calls are refused at admission.
        """
        if reason in self._stopped:
            return
        running = {n: t for n, t in self._tasks.items() if not t.done()}
        if not running:
            return
        self._stopped.add(reason)
        self._log(f"{reason}; stopping {len(running)} agent() call(s) in flight")
        for n, task in running.items():
            self._budget_cancelled.add(n)
            self._stop_reason[n] = reason
            cause = self._causes.get(n)
            if cause is not None:
                # Before the cancel: the dispatcher reads it to tell this stop from the host's.
                cause.reason = reason
            task.cancel()

    def _settle_live(self, n: int, result: AgentResult | None = None) -> int:
        """Fold call *n* into the counters (its result, else what it last reported); returns
        the output tokens added."""
        reported = self._live.pop(n, None)
        self._tasks.pop(n, None)
        self._causes.pop(n, None)
        if result is not None:
            usage, cost = _settled_usage(result.usage, result.cost_usd, reported)
            added = self._budget.add_result(usage, cost)
            if self._quota is not None:
                self._quota.charge(self._tokens_of(usage), _money(cost))
        elif reported is not None:
            added = self._budget.add_result(reported, _money(reported.get(COST_USD)) or None)
            if self._quota is not None:
                self._quota.charge(self._tokens_of(reported), _money(reported.get(COST_USD)))
        else:
            added = 0
        self._check_live()
        return added

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
        self._check_budget(call.ordinal)
        self._check_quota(call.ordinal)
        if self._budget.total is not None:
            r = self._reserve
            if r is None:
                r = self._finished_out // self._finished_n if self._finished_n else 0
            self._reserved[call.ordinal] = r
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
        try:
            if self._aborted:
                self._log(f"agent() #{n} aborted")
                return None
            async with self._slots:
                if self._aborted:
                    self._log(f"agent() #{n} aborted")
                    return None
                # Hard ceiling also for calls that queued behind the cap.
                self._check_budget(n)
                self._check_quota(n)
                value: Any = None
                if self._on_agent_start is not None:
                    self._on_agent_start(call)
                try:
                    value = await self._dispatch(call)
                finally:
                    if self._on_agent_end is not None:
                        self._on_agent_end(call, value)
                return value
        finally:
            self._reserved.pop(n, None)

    async def _dispatch(self, call: AgentCall) -> Any:
        n = call.ordinal
        key = self._keys.pop(n, None) or self._key(call)
        reported = False
        written: DispatchHandle | None = None

        def report(handle: DispatchHandle | None) -> None:
            nonlocal reported, written
            # The first handle is written; a later one only when it names another place (a
            # fallback's provider, a retry attempt's chat or session), and the journal keeps the
            # last, so a reattach points at the attempt that was live when the run was cut off.
            if reported and (handle is None or handle == written):
                return
            reported = True
            if self._journal is not None:
                h = handle if handle is not None else DispatchHandle()
                written = h
                self._journal.append_started(
                    StartRecord(n, key, h.provider, h.chat, h.session, h.worker_chat)
                )

        reports = (
            isinstance(self._dispatcher, ReportsDispatch) and self._dispatcher.reports_dispatch
        )
        if not reports:
            report(None)
        # Set around create_task only, so just the task's copied context sees it.
        handle = self._reattach.pop(n, None)

        # Each attempt's latest cumulative usage, by slot; the call's live usage sums them.
        parts: list[Mapping[str, Any]] = []

        def open_slot() -> Callable[[Mapping[str, Any]], None]:
            slot = len(parts)
            parts.append({})

            def usage_report(usage: Mapping[str, Any]) -> None:
                # A late report from a call that is over changes nothing.
                if n in self._tasks:
                    parts[slot] = dict(usage)
                    self._live[n] = _sum_usage(parts)
                    self._check_live()

            return usage_report

        cause = StopCause()
        self._causes[n] = cause
        token = _DISPATCH_REPORT.set(report)
        reattach_token = _REATTACH.set(handle)
        # A dispatcher that never opens an attempt reports into the call's first slot.
        usage_token = _USAGE_REPORT.set(open_slot())
        attempt_token = _USAGE_ATTEMPT.set(open_slot)
        cause_token = _STOP_CAUSE.set(cause)
        try:
            task = asyncio.create_task(self._dispatcher.run_agent(call))
        finally:
            _STOP_CAUSE.reset(cause_token)
            _USAGE_ATTEMPT.reset(attempt_token)
            _USAGE_REPORT.reset(usage_token)
            _REATTACH.reset(reattach_token)
            _DISPATCH_REPORT.reset(token)
        self._inflight.add(task)
        self._tasks[n] = task
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
        if task.cancelled() and n in self._budget_cancelled:
            # The hard cap stopped it: it throws in the script like a refused call, and a
            # resume runs it again (the record is not replayed).
            reason = self._stop_reason[n]
            live_output = _whole((self._live.get(n) or {}).get(OUTPUT_TOKENS))
            self._settle_live(n)
            self._log(f"agent() #{n} stopped: {reason}")
            self._record(
                AgentRecord(
                    n, key, "error", None, live_output, error=f"stopped: {reason}", kind="budget"
                )
            )
            raise BudgetExhaustedError(reason)
        if task.cancelled():
            # A dispatcher that cancels its own turn is a skip (plan §4.4).
            self._log(f"agent() #{n} {'aborted' if self._aborted else 'skipped'}")
            added = self._settle_live(n)
            # An aborted call did not complete; resume reruns it.
            if not self._aborted:
                self._record(AgentRecord(n, key, "null", None, added))
            return None
        exc = task.exception()
        if exc is not None:
            self._log(f"agent() #{n} failed: {exc}")
            kind = exc.kind if isinstance(exc, DispatchError) else "failed"
            added = self._settle_live(n)
            self._record(AgentRecord(n, key, "error", None, added, error=str(exc), kind=kind))
            return None
        result = task.result()
        added = self._settle_live(n, result)
        self._finished_out += _whole(result.usage.get(OUTPUT_TOKENS))
        self._finished_n += 1
        try:
            json.dumps(result.value, allow_nan=False)
        except (TypeError, ValueError) as err:
            self._log(f"agent() #{n} failed: result is not JSON: {err}")
            self._record(
                AgentRecord(
                    n, key, "error", None, added, error=f"result is not JSON: {err}", kind="failed"
                )
            )
            return None
        schema = call.opts.get("schema")
        if isinstance(schema, Mapping) and result.value is not None:
            error = schema_error(schema, result.value)
            if error is not None:
                self._log(f"agent() #{n} failed: {error}")
                self._record(AgentRecord(n, key, "error", None, added, error=error, kind="failed"))
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
        if self._wall_timer is not None:
            self._wall_timer.cancel()
            self._wall_timer = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
