"""The workflow verbs, typed for editors and type checkers.

Inside a run, the Python runner gives a workflow file these same verbs as globals,
so `from ahp_host_flow.verbs import agent` is optional. Called outside a run they
raise `OutsideRunnerError`. Standard library and `pyrunner` only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ahp_host_flow.sandbox import pyrunner
from ahp_host_flow.sandbox.pyrunner import MISSING

if TYPE_CHECKING:
    args: Any

__all__ = [
    "OutsideRunnerError",
    "agent",
    "approve",
    "args",
    "ask",
    "budget",
    "canvas",
    "log",
    "parallel",
    "phase",
    "pipeline",
    "verb",
    "workflow",
]


class OutsideRunnerError(RuntimeError):
    """A verb was called outside a workflow run by the Python runner."""


def _runtime(name: str) -> pyrunner.Runtime:
    rt = pyrunner.current_runtime()
    if rt is None:
        raise OutsideRunnerError(
            f"ahp_host_flow.verbs.{name}() only works inside a workflow run by the Python "
            "runner; a workflow file gets the verbs as globals"
        )
    return rt


def agent(prompt: str, **opts: Any) -> Awaitable[Any]:
    """Run one inner agent; the awaitable gives its result, or None when skipped."""
    return _runtime("agent").agent(prompt, **opts)


def parallel(thunks: Sequence[Callable[[], Any]]) -> Awaitable[list[Any]]:
    """Call every thunk now and wait for all; a thunk that raises gives None."""
    return _runtime("parallel").parallel(thunks)


def pipeline(items: Sequence[Any], *stages: Callable[[Any, Any, int], Any]) -> Awaitable[list[Any]]:
    """Flow each item through `stage(prev, item, index)`; a raise drops the item to None."""
    return _runtime("pipeline").pipeline(items, *stages)


def phase(title: str) -> None:
    _runtime("phase").phase(title)


def log(message: str) -> None:
    _runtime("log").log(message)


def workflow(name_or_ref: str | Mapping[str, str], args: Any = MISSING) -> Awaitable[Any]:
    """Run a child workflow (one level of nesting) and return its value."""
    rt = _runtime("workflow")
    return rt.workflow(name_or_ref) if args is MISSING else rt.workflow(name_or_ref, args)


def ask(
    question: str,
    *,
    choices: Sequence[str] | None = None,
    schema: Mapping[str, Any] | None = None,
) -> Awaitable[Any]:
    """Ask a human; the awaitable gives the answer, or None when declined or aborted."""
    opts: dict[str, Any] = {}
    if choices is not None:
        opts["choices"] = choices
    if schema is not None:
        opts["schema"] = schema
    rt = _runtime("ask")
    if "ask" in rt.verbs:
        return pyrunner.failed(TypeError(_replaced("ask")))
    return rt.ask(question, **opts)


def _replaced(name: str) -> str:
    return (
        f"{name}() is a custom verb in this run: call it as a global or with "
        f"ahp_host_flow.verbs.verb({name!r}, ...)"
    )


def approve(
    summary: str, *, diff: str | Sequence[Mapping[str, Any]] | None = None
) -> Awaitable[bool]:
    """Ask a human to approve; the awaitable gives True, or False when denied."""
    opts: dict[str, Any] = {}
    if diff is not None:
        opts["diff"] = diff
    rt = _runtime("approve")
    if "approve" in rt.verbs:
        return pyrunner.failed(TypeError(_replaced("approve")))
    return rt.approve(summary, **opts)


def canvas(url: str, *, title: str | None = None) -> None:
    """Show an http(s) page as a live view on the run's main chat."""
    opts: dict[str, Any] = {}
    if title is not None:
        opts["title"] = title
    rt = _runtime("canvas")
    if "canvas" in rt.verbs:
        raise TypeError(_replaced("canvas"))
    rt.canvas(url, **opts)


def verb(name: str, *args: Any) -> Awaitable[Any]:
    """Call a custom verb of this run by name; the awaitable gives the handler's JSON value."""
    return _runtime("verb").verb(name, *args)


class _Budget:
    @property
    def total(self) -> int | None:
        return _runtime("budget").budget.total

    def spent(self) -> int:
        return _runtime("budget").budget.spent()

    def remaining(self) -> float:
        return _runtime("budget").budget.remaining()


budget = _Budget()


def __getattr__(name: str) -> Any:
    if name == "args":
        return _runtime("args").args
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
