"""Human gates for a run: `ask()`, `approve()` and `canvas()` (plan §6).

The engine knows nothing about chats. A `Gates` implementation (the provider's, or a
test fake) raises the request on whatever surface the run has; `GateRunner` numbers
the calls, re-validates what comes back and keeps abort and masking out of it.
Standard library, `journal`, `abort` and `schema` only (`resume.GateReplay` for types).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from ahp_host_flow.engine.abort import AbortSignal
from ahp_host_flow.engine.journal import GateRecord, JournalWriter, canonical_json
from ahp_host_flow.engine.schema import schema_error

if TYPE_CHECKING:
    from ahp_host_flow.engine.resume import GateReplay

GATE_KINDS: Final = ("ask", "approve")

_HTTP_URL: Final = re.compile(r"(?i)https?://\S")


@dataclass(frozen=True)
class GateCall:
    """One `ask()` or `approve()` call; `ordinal` counts gates only, never `agent()` calls."""

    ordinal: int
    kind: str
    # The ask() question, or the approve() summary.
    question: str
    # Only the keys the script set: ask -> choices and/or schema; approve -> diff.
    options: Mapping[str, Any]


def _optional_text(value: object) -> bool:
    return value is None or isinstance(value, str)


def options_error(kind: str, options: Mapping[str, Any]) -> str | None:
    """Why a gate's options are malformed, or None.

    The host is the authority: a runner checks the same shapes, but a script can
    make its runner send anything (a `toJSON`, a replaced `JSON.stringify`).
    """
    allowed = ("choices", "schema") if kind == "ask" else ("diff", "x")
    extra = sorted(str(k) for k in options if k not in allowed)
    if extra:
        return f"{kind}() options: unknown key(s) {', '.join(extra)}"
    if kind == "ask":
        choices, schema = options.get("choices"), options.get("schema")
        if "choices" in options and not (
            isinstance(choices, list) and choices and all(isinstance(c, str) for c in choices)
        ):
            return "ask() choices must be a non-empty array of strings"
        if "schema" in options and not isinstance(schema, Mapping):
            return "ask() schema must be an object"
        if "choices" in options and "schema" in options:
            return "ask() takes choices or schema, not both"
        return None
    if "diff" in options:
        diff = options["diff"]
        if not isinstance(diff, str) and not (
            isinstance(diff, list)
            and all(
                isinstance(d, Mapping)
                and isinstance(d.get("path"), str)
                and _optional_text(d.get("before"))
                and _optional_text(d.get("after"))
                for d in diff
            )
        ):
            return "approve() diff must be a string or an array of {path, before?, after?}"
    if "x" in options:
        x = options["x"]
        if not (
            isinstance(x, Mapping)
            and set(x) <= {"detail"}
            and isinstance(x.get("detail", False), bool)
        ):
            return "approve() x must be an object of {detail: boolean}"
    return None


def wants_detail(options: Mapping[str, Any]) -> bool:
    """Whether an approve() asked for `{approved, reason, answeredBy}` instead of a bool."""
    x = options.get("x")
    return isinstance(x, Mapping) and x.get("detail") is True


def gate_key(kind: str, question: str, options: Mapping[str, Any]) -> str:
    """Identify a gate across runs by what it asks, not by when."""
    payload = {"kind": kind, "options": dict(options), "question": question}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@runtime_checkable
class Gates(Protocol):
    async def ask(self, call: GateCall) -> Any:
        """The answer as JSON, or None when the question was declined."""
        ...

    async def approve(self, call: GateCall) -> bool: ...

    async def canvas(self, url: str, title: str | None) -> None: ...


class GateUnavailableError(Exception):
    """No client can answer a gate in this run."""


def no_answer_path(kind: str) -> str:
    return f"{kind}() has no answer path in this run: no client can answer a human gate here"


def _approval_parts(answer: Any) -> tuple[bool, str | None, str | None]:
    """An approve() answer as (approved, reason, answered_by); a bare bool has neither."""
    if isinstance(answer, bool):
        return answer, None, None
    reason = getattr(answer, "reason", None)
    by = getattr(answer, "answered_by", None)
    return (
        getattr(answer, "approved", False) is True,
        reason if isinstance(reason, str) else None,
        by if isinstance(by, str) else None,
    )


class GateRunner:
    """Numbers a run's gates and runs them through `gates`, with abort, masking and logs."""

    def __init__(
        self,
        gates: Gates | None,
        *,
        abort: AbortSignal | None = None,
        mask: Callable[[Any], Any] | None = None,
        on_log: Callable[[str], None] | None = None,
        journal: JournalWriter | None = None,
        replay: GateReplay | None = None,
    ) -> None:
        self._gates = gates
        self._journal = journal
        self._replay = replay
        self._mask = mask
        self._on_log = on_log
        self._next = 0
        self._tasks: set[asyncio.Future[Any]] = set()
        self._canvas_warned = False
        self.aborted = False
        self._unsubscribe = abort.subscribe(self._on_abort) if abort is not None else None

    def _on_abort(self) -> None:
        self.aborted = True
        for task in list(self._tasks):
            task.cancel()

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def issue(
        self, kind: object, question: object, options: object
    ) -> tuple[GateCall, GateRecord | None]:
        """Take the next gate ordinal and look up its replayed answer, if any.

        Called when the request line arrives, so both follow line order.
        """
        if (
            not isinstance(kind, str)
            or kind not in GATE_KINDS
            or not isinstance(question, str)
            or not isinstance(options, Mapping)
        ):
            raise ValueError("bad gate request")
        reason = options_error(kind, options)
        if reason is not None:
            raise ValueError(reason)
        call = GateCall(self._next, kind, question, dict(options))
        self._next += 1
        cached = self._replay.lookup(call) if self._replay is not None else None
        return call, cached

    def _append(self, record: GateRecord) -> None:
        if self._journal is not None:
            self._journal.append_gate(record)

    async def run(self, call: GateCall, cached: GateRecord | None = None) -> Any:
        """The gate's answer: a value (ask), a bool (approve; its detail object with `x.detail`);
        None/False when aborted.

        A `cached` record (an earlier run's answer) is journaled again and returned
        without asking, even after an abort: the answer is already known.
        """
        detail = call.kind == "approve" and wants_detail(call.options)
        if cached is not None:
            self._append(cached)
            if call.kind != "approve":
                return cached.answer
            answer = cached.answer
            if detail:
                if isinstance(answer, Mapping):
                    return answer
                return {"approved": answer is True, "reason": None, "answeredBy": None}
            if isinstance(answer, Mapping):
                return answer.get("approved") is True
            return answer is True
        declined: Any = None if call.kind == "ask" else False
        if detail:
            declined = {"approved": False, "reason": None, "answeredBy": None}
        aborted = f"{call.kind}() #{call.ordinal} aborted"
        if self.aborted:
            self._log(aborted)
            return declined
        gates = self._gates
        if gates is None:
            raise GateUnavailableError(no_answer_path(call.kind))
        key = gate_key(call.kind, call.question, call.options)
        self._append(GateRecord(call.ordinal, key, call.kind, "open"))
        outcome_fn = getattr(gates, "approve_outcome", None)
        approve_fn = outcome_fn if callable(outcome_fn) else gates.approve
        task: asyncio.Future[Any] = asyncio.ensure_future(
            gates.ask(call) if call.kind == "ask" else approve_fn(call)
        )
        self._tasks.add(task)
        try:
            answer = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self.aborted and (current is None or current.cancelling() == 0):
                self._log(aborted)
                return declined
            raise
        finally:
            self._tasks.discard(task)
            if not task.done():
                task.cancel()
        if call.kind == "approve":
            approved, why, by = _approval_parts(answer)
            if not detail:
                self._append(GateRecord(call.ordinal, key, call.kind, "answered", approved))
                return approved
            if self._mask is not None and why is not None:
                why = self._mask(why)
            value = {"approved": approved, "reason": why, "answeredBy": by}
            self._append(GateRecord(call.ordinal, key, call.kind, "answered", value))
            return value
        reason = self._reject(call, answer)
        if reason is not None:
            self._log(f"ask() #{call.ordinal}: answer rejected: {reason}")
            answer = None
        # Masked once, here, so a resumed run replays the value the script saw.
        value = self._mask(answer) if self._mask is not None else answer
        self._append(GateRecord(call.ordinal, key, call.kind, "answered", value))
        return value

    @staticmethod
    def _reject(call: GateCall, answer: Any) -> str | None:
        if answer is None:
            return None
        choices = call.options.get("choices")
        schema = call.options.get("schema")
        if choices is not None:
            if not isinstance(answer, str) or answer not in choices:
                return f"{answer!r} is not one of the choices"
            return None
        if isinstance(schema, Mapping):
            return schema_error(schema, answer)
        return None if isinstance(answer, str) else "answer is not a string"

    async def canvas(self, url: object, title: object) -> None:
        if (
            not isinstance(url, str)
            or not _HTTP_URL.match(url)
            or not (title is None or isinstance(title, str))
        ):
            self._log("canvas(): ignored: not an http(s) URL")
            return
        if self._gates is None:
            if not self._canvas_warned:
                self._canvas_warned = True
                self._log("canvas(): no client can show a canvas in this run; ignored")
            return
        try:
            await self._gates.canvas(url, title)
        except Exception as exc:
            self._log(f"canvas() failed: {exc}")
