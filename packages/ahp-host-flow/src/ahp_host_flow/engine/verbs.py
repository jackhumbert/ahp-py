"""Custom verbs for a run: `tool()`, `render()` and whatever else a deployment declares.

A verb is a name the script can call and a handler the deployment runs, with no model
involved (plan M12). `VerbRunner` numbers the calls, journals them, masks what leaves
the run and keeps abort out of the handler. The engine knows nothing about chats: a
`VerbHost` (the provider's, or a test fake) supplies the call's context and shows its
progress. Standard library, `journal`, `abort` and `gates` only.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ahp_host_flow.engine.abort import AbortSignal
from ahp_host_flow.engine.gates import GateUnavailableError, no_answer_path
from ahp_host_flow.engine.journal import JournalWriter, VerbRecord, verb_call_id, verb_key

if TYPE_CHECKING:
    from ahp_host_flow.engine.resume import VerbReplay


@dataclass(frozen=True)
class AskOutcome:
    """The human's answer as JSON (None when declined) and who gave it, when the host knows."""

    value: Any
    answered_by: str | None = None


@dataclass(frozen=True)
class ConfirmOutcome:
    """Whether the human approved and who answered, when the host knows; truthy as approved."""

    approved: bool
    answered_by: str | None = None

    def __bool__(self) -> bool:
        return self.approved


class AskFn(Protocol):
    def __call__(
        self, question: str, options: Mapping[str, Any] | None = None
    ) -> Awaitable[AskOutcome]:
        """The human's answer, or a None value when the question was declined."""
        ...


class ConfirmFn(Protocol):
    def __call__(
        self, summary: str, options: Mapping[str, Any] | None = None
    ) -> Awaitable[ConfirmOutcome]:
        """Whether the human approved."""
        ...


async def _no_ask(question: str, options: Mapping[str, Any] | None = None) -> AskOutcome:
    raise GateUnavailableError(no_answer_path("ask"))


async def _no_confirm(summary: str, options: Mapping[str, Any] | None = None) -> ConfirmOutcome:
    raise GateUnavailableError(no_answer_path("confirm"))


def _no_log(message: str) -> None:
    return None


@dataclass(frozen=True)
class VerbContext:
    """What a handler may use besides its arguments; the defaults are a run with no client."""

    session: str | None = None
    chat: str | None = None
    principal: str | None = None
    log: Callable[[str], None] = _no_log
    ask: AskFn = _no_ask
    confirm: ConfirmFn = _no_confirm


@dataclass(frozen=True)
class VerbCall:
    """One custom verb call; `ordinal` counts verbs only, never `agent()` or gate calls."""

    name: str
    args: tuple[Any, ...]
    ordinal: int
    # Stable across a resume: derived from the name, the args and the occurrence.
    call_id: str
    phase: str | None
    run_id: str | None
    context: VerbContext


# A handler may also return a plain value; `VerbRunner` awaits only what is awaitable.
VerbHandler = Callable[[VerbCall], Awaitable[Any]]


@runtime_checkable
class VerbHost(Protocol):
    def context(self, ordinal: int, call_id: str) -> VerbContext: ...

    async def started(self, call: VerbCall, args: Sequence[Any]) -> None:
        """The call began; *args* are masked."""
        ...

    async def finished(self, call: VerbCall, *, ok: bool, value: Any, error: str | None) -> None:
        """The call ended; *value* and *error* are masked."""
        ...


class VerbFailedError(RuntimeError):
    """A verb call failed; the message is what the verb throws in the script."""


class VerbRunner:
    """Numbers a run's custom verb calls and runs them through their handlers.

    Verbs never touch the `Scheduler`: no agent cap, no budget, no concurrency limit.
    """

    def __init__(
        self,
        handlers: Mapping[str, VerbHandler],
        *,
        host: VerbHost | None = None,
        abort: AbortSignal | None = None,
        mask: Callable[[Any], Any] | None = None,
        on_log: Callable[[str], None] | None = None,
        journal: JournalWriter | None = None,
        replay: VerbReplay | None = None,
        run_id: str | None = None,
        phase_of: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._handlers = dict(handlers)
        self._host = host
        self._mask = mask
        self._on_log = on_log
        self._journal = journal
        self._replay = replay
        self._run_id = run_id
        self._phase_of = phase_of
        self._next = 0
        self._occurrences: dict[str, int] = {}
        self._tasks: set[asyncio.Future[Any]] = set()
        self.aborted = False
        self._unsubscribe = abort.subscribe(self._on_abort) if abort is not None else None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

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

    def issue(self, name: object, args: object) -> tuple[VerbCall, VerbRecord | None]:
        """Take the next verb ordinal and look up its replayed result, if any.

        Called when the request line arrives, so ordinals follow line order. The host is
        the authority: a runner can be made to send anything, so a request for a verb
        this run did not declare is refused and takes no ordinal.
        """
        if not isinstance(name, str) or name not in self._handlers:
            declared = ", ".join(self.names) or "none"
            raise ValueError(f"unknown verb {name!r}: this run declares {declared}")
        if not isinstance(args, list):
            raise ValueError("verb args must be an array")
        try:
            key = verb_key(name, args)
        except (TypeError, ValueError) as exc:
            raise ValueError("verb args must be JSON") from exc
        occurrence = self._occurrences.get(key, 0)
        self._occurrences[key] = occurrence + 1
        call_id = verb_call_id(key, occurrence)
        ordinal = self._next
        self._next += 1
        if self._host is not None:
            context = self._host.context(ordinal, call_id)
        else:
            context = VerbContext(log=self._on_log or _no_log)
        phase = self._phase_of()
        call = VerbCall(name, tuple(args), ordinal, call_id, phase, self._run_id, context)
        cached = self._replay.lookup(call_id, key, ordinal) if self._replay is not None else None
        if cached is None and self._replay is not None and self._replay.was_open(call_id):
            self._log(
                f"resume: {name}() #{ordinal} was cut off before it finished; "
                f"calling its handler again with call id {call_id}"
            )
        return call, cached

    def _append(self, record: VerbRecord) -> None:
        if self._journal is not None:
            self._journal.append_verb(record)

    async def _finished(
        self, call: VerbCall, *, ok: bool, value: Any = None, error: str | None = None
    ) -> None:
        if self._host is None:
            return
        try:
            await self._host.finished(call, ok=ok, value=value, error=error)
        except Exception as exc:
            self._log(f"{call.name}() #{call.ordinal}: progress failed: {exc}")

    async def _invoke(self, handler: VerbHandler, call: VerbCall) -> Any:
        result: Any = handler(call)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _fail(self, call: VerbCall, key: str, message: str) -> VerbFailedError:
        self._append(VerbRecord(call.ordinal, key, call.name, call.call_id, "error", error=message))
        await self._finished(call, ok=False, error=message)
        return VerbFailedError(message)

    async def run(self, call: VerbCall, cached: VerbRecord | None = None) -> Any:
        """The handler's return value, masked; a failure raises `VerbFailedError`.

        A `cached` record (an earlier run's result) is journaled again and served
        without calling the handler, even after an abort: the result is already known.
        """
        name, n = call.name, call.ordinal
        if cached is not None:
            self._append(cached)
            if cached.status == "done":
                return cached.value
            raise VerbFailedError(cached.error or f"{name}() failed")
        if self.aborted:
            self._log(f"{name}() #{n} aborted")
            raise VerbFailedError(f"{name}() aborted")
        key = verb_key(name, call.args)
        mask = self._mask
        self._append(VerbRecord(n, key, name, call.call_id, "open"))
        if self._host is not None:
            masked_args = mask(list(call.args)) if mask is not None else list(call.args)
            try:
                await self._host.started(call, masked_args)
            except Exception as exc:
                self._log(f"{name}() #{n}: progress failed: {exc}")
        task: asyncio.Future[Any] = asyncio.ensure_future(self._invoke(self._handlers[name], call))
        self._tasks.add(task)
        try:
            result = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self.aborted and (current is None or current.cancelling() == 0):
                # Only the open record stays, so a resume calls the handler again.
                self._log(f"{name}() #{n} aborted")
                await self._finished(call, ok=False, error=f"{name}() aborted")
                raise VerbFailedError(f"{name}() aborted") from None
            raise
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            failure = await self._fail(call, key, mask(message) if mask is not None else message)
            raise failure from None
        finally:
            self._tasks.discard(task)
            if not task.done():
                task.cancel()
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as err:
            message = f"{name}() returned a value that is not JSON: {err}"
            failure = await self._fail(call, key, mask(message) if mask is not None else message)
            raise failure from None
        # Masked once, here, so the script, the journal and a resumed run get the same value.
        value = mask(result) if mask is not None else result
        self._append(VerbRecord(n, key, name, call.call_id, "done", value))
        await self._finished(call, ok=True, value=value)
        return value
