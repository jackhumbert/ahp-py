"""The engine's one outward call: an `agent()` call in, its result out."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentCall:
    # 0-based, assigned in JS when agent() is called, not when it completes (plan §9).
    ordinal: int
    prompt: str
    # agent() opts verbatim, unknown keys included (plan §3 probe).
    opts: Mapping[str, Any] = field(default_factory=dict)
    # opts.phase if a string, else the latest phase() title at issue time; set in JS
    # at issue time so a progress listener can group calls (plan §3 phase row).
    phase: str | None = None
    # opts.label if a string; set in JS at issue time (plan §3 phase row).
    label: str | None = None


@dataclass(frozen=True)
class AgentResult:
    # JSON value handed back to the script; None means agent() returns null.
    value: Any
    # Token counts; usage["output_tokens"] is summed into the budget.
    usage: Mapping[str, int] = field(default_factory=dict)


@runtime_checkable
class Dispatcher(Protocol):
    async def run_agent(self, call: AgentCall) -> AgentResult: ...


class EchoDispatcher:
    """Answers every agent() call with its own prompt. No model, no network."""

    def __init__(self) -> None:
        self.calls: list[AgentCall] = []

    async def run_agent(self, call: AgentCall) -> AgentResult:
        self.calls.append(call)
        return AgentResult(value=call.prompt)
