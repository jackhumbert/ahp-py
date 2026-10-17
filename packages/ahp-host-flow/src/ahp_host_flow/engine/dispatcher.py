"""The engine's one outward call: an `agent()` call in, its result out."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


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
    # Token counts; usage["output_tokens"] is summed into the budget.
    usage: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchHandle:
    """Where a call runs, as far as the dispatcher can say (journal start record)."""

    provider: str | None = None
    chat: str | None = None


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


class EchoDispatcher:
    """Answers every agent() call with its own prompt. No model, no network."""

    def __init__(self) -> None:
        self.calls: list[AgentCall] = []

    async def run_agent(self, call: AgentCall) -> AgentResult:
        self.calls.append(call)
        return AgentResult(value=call.prompt)
