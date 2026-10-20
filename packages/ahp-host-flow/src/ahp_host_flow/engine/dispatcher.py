"""The engine's one outward call: an `agent()` call in, its result out."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, runtime_checkable

ErrorKind = Literal[
    "unavailable", "rate_limited", "refused", "timeout", "empty", "failed", "budget"
]
ERROR_KINDS: Final = (
    "unavailable",
    "rate_limited",
    "refused",
    "timeout",
    "empty",
    "failed",
    "budget",
)


class DispatchError(RuntimeError):
    """A dispatcher's failure, classified. `str(exc)` is the message alone.

    `kind` says what went wrong and `transient` whether a retry could help. Only the
    Scheduler sets 'budget'; a dispatcher never does.
    """

    def __init__(self, kind: ErrorKind, transient: bool, message: str) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.transient = transient


@dataclass(frozen=True)
class AgentCall:
    # 0-based, assigned by the host Scheduler.issue when the request arrives (issue order
    # within one runner), not when the call completes (plan §9).
    ordinal: int
    prompt: str
    # agent() opts verbatim, unknown keys included (plan §3 probe).
    opts: Mapping[str, Any] = field(default_factory=dict)
    # opts.phase if a string, else the run's current phase when Scheduler.issue ran, so a
    # progress listener can group calls (plan §3 phase row).
    phase: str | None = None
    # opts.label if a string; passed on the request and kept at issue (plan §3 phase row).
    label: str | None = None


@dataclass(frozen=True)
class AgentResult:
    # JSON value handed back to the script; None means agent() returns null.
    value: Any
    # Token counts ('input_tokens', 'output_tokens', 'cache_read_tokens'); the budget sums them.
    usage: Mapping[str, int] = field(default_factory=dict)
    # The call's cost in USD when the agent reported one; summed into the cost budget.
    cost_usd: float | None = None


@dataclass(frozen=True)
class DispatchHandle:
    """Where a call runs, as far as the dispatcher can say (journal start record)."""

    provider: str | None = None
    chat: str | None = None
    # The node session URI of a fleet call, or the remote session of a remote call.
    session: str | None = None


@runtime_checkable
class Dispatcher(Protocol):
    async def run_agent(self, call: AgentCall) -> AgentResult: ...


@runtime_checkable
class ReportsDispatch(Protocol):
    """A dispatcher that calls `report_dispatch` once per call, as soon as it knows."""

    reports_dispatch: bool


_DISPATCH_REPORT: ContextVar[Callable[[DispatchHandle], None] | None] = ContextVar(
    "_DISPATCH_REPORT", default=None
)


def report_dispatch(handle: DispatchHandle) -> None:
    """Tell the scheduler where this call runs; a no-op outside a scheduler dispatch."""
    report = _DISPATCH_REPORT.get()
    if report is not None:
        report(handle)


_USAGE_REPORT: ContextVar[Callable[[Mapping[str, Any]], None] | None] = ContextVar(
    "_USAGE_REPORT", default=None
)
#: Opens a fresh usage slot of the current call (one per dispatch attempt); set by the Scheduler.
_USAGE_ATTEMPT: ContextVar[Callable[[], Callable[[Mapping[str, Any]], None]] | None] = ContextVar(
    "_USAGE_ATTEMPT", default=None
)


def _no_usage_report(usage: Mapping[str, Any]) -> None:
    return None


def usage_reporter() -> Callable[[Mapping[str, Any]], None]:
    """The hook that tells the scheduler what this attempt has used so far; a no-op elsewhere.

    Capture it in the dispatch task (the context the Scheduler set) and call it from anywhere,
    e.g. a worker turn's callback that runs in the host's task. The mapping is the attempt's
    cumulative usage so far (keys output_tokens, input_tokens, cache_read_tokens ints,
    cost_usd float); the hard budget cap stops the call when a counter passes its limit.
    Each attempt opened with `begin_usage_attempt` has a slot of its own and the Scheduler
    sums the slots, so a retried or fallen-back call counts every attempt.
    """
    report = _USAGE_REPORT.get()
    return report if report is not None else _no_usage_report


def begin_usage_attempt() -> tuple[Callable[[Mapping[str, Any]], None], Token[Any] | None]:
    """Open a new usage slot for one attempt of the current call and make it the reporter.

    Returns the slot's reporter and the token to `end_usage_attempt` with. Outside a
    Scheduler dispatch the reporter is a no-op and the token None.
    """
    opener = _USAGE_ATTEMPT.get()
    if opener is None:
        return _no_usage_report, None
    report = opener()
    return report, _USAGE_REPORT.set(report)


def end_usage_attempt(token: Token[Any] | None) -> None:
    if token is not None:
        _USAGE_REPORT.reset(token)


@dataclass
class StopCause:
    """Why a call's task is being cancelled, set before the cancellation reaches it.

    The Scheduler sets `reason` when its hard cap (a budget, the wall clock, a quota) stops the
    call; a dispatcher's own `call_timeout` scope is `timeout`. A cancellation with neither (and
    no abort) is the host stopping. Read it with `stop_cause()` inside the dispatch task.
    """

    reason: str | None = None
    timeout: asyncio.Timeout | None = None

    def stopped(self) -> bool:
        """True when this run itself stopped the call (budget, quota, wall time or timeout)."""
        return self.reason is not None or (self.timeout is not None and self.timeout.expired())


_STOP_CAUSE: ContextVar[StopCause | None] = ContextVar("_STOP_CAUSE", default=None)


def stop_cause() -> StopCause | None:
    """The current call's `StopCause`, else None (outside a Scheduler dispatch)."""
    return _STOP_CAUSE.get()


_REATTACH: ContextVar[DispatchHandle | None] = ContextVar("_REATTACH", default=None)


def reattach_handle() -> DispatchHandle | None:
    """The handle a continued run recorded for this call when it was cut off, else None."""
    return _REATTACH.get()


class EchoDispatcher:
    """Answers every agent() call with its own prompt. No model, no network."""

    def __init__(self) -> None:
        self.calls: list[AgentCall] = []

    async def run_agent(self, call: AgentCall) -> AgentResult:
        self.calls.append(call)
        return AgentResult(value=call.prompt)
