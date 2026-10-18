"""Custom verb fakes for the hook tests; not a test module. Engine imports only."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from ahp_host_flow.engine import VerbCall, VerbContext, VerbHandler

# Call id of every echo_verb call, in call order.
ECHO_CALLS: list[str] = []


async def echo_verb(call: VerbCall) -> dict[str, Any]:
    ECHO_CALLS.append(call.call_id)
    return {"name": call.name, "args": list(call.args)}


async def fail_verb(call: VerbCall) -> Any:
    raise RuntimeError(f"{call.name} failed: " + ", ".join(json.dumps(a) for a in call.args))


async def hang_verb(call: VerbCall) -> Any:
    await asyncio.Event().wait()


class HoldVerb:
    """State of `hold_verb`: while `holding`, a call never returns."""

    # Call id of every hold_verb call, in call order.
    calls: ClassVar[list[str]] = []
    holding: bool = True


async def hold_verb(call: VerbCall) -> Any:
    HoldVerb.calls.append(call.call_id)
    while HoldVerb.holding:
        await asyncio.Event().wait()
    return "released"


async def ask_verb(call: VerbCall) -> Any:
    return await call.context.ask("which?")


async def asking_verb(call: VerbCall) -> Any:
    return {
        "answer": await call.context.ask("Pick a colour", {"choices": ["red", "blue"]}),
        "session": call.context.session,
        "chat": call.context.chat,
    }


async def confirming_verb(call: VerbCall) -> Any:
    return await call.context.confirm("Deploy now?")


async def bad_ask_verb(call: VerbCall) -> Any:
    return await call.context.ask("q", {"choices": []})


def counting_verb(counter: list[str]) -> VerbHandler:
    """An echo handler that appends each call's id to *counter*."""

    async def handler(call: VerbCall) -> dict[str, Any]:
        counter.append(call.call_id)
        return {"name": call.name, "args": list(call.args)}

    return handler


_KINDS: dict[str, VerbHandler] = {
    "echo": echo_verb,
    "fail": fail_verb,
    "hang": hang_verb,
    "ask": ask_verb,
}


class FakeVerbs:
    """Handlers by verb name from `{name: 'echo' | 'fail' | 'hang' | 'ask'}`, recording calls."""

    def __init__(self, kinds: Mapping[str, str]) -> None:
        self.calls: list[VerbCall] = []
        self.handlers: dict[str, VerbHandler] = {
            name: self._recording(_KINDS[kind]) for name, kind in kinds.items()
        }

    def _recording(self, inner: VerbHandler) -> VerbHandler:
        async def handler(call: VerbCall) -> Any:
            self.calls.append(call)
            return await inner(call)

        return handler


class FakeVerbHost:
    def __init__(self) -> None:
        self.started_calls: list[tuple[VerbCall, list[Any]]] = []
        self.finished_calls: list[tuple[VerbCall, bool, Any, str | None]] = []
        self.logs: list[str] = []

    def context(self, ordinal: int, call_id: str) -> VerbContext:
        return VerbContext(log=self.logs.append)

    async def started(self, call: VerbCall, args: Sequence[Any]) -> None:
        self.started_calls.append((call, list(args)))

    async def finished(self, call: VerbCall, *, ok: bool, value: Any, error: str | None) -> None:
        self.finished_calls.append((call, ok, value, error))


def reset_hooks() -> None:
    ECHO_CALLS.clear()
    HoldVerb.calls.clear()
    HoldVerb.holding = True


def route_key(call: Any, route: Mapping[str, Any]) -> dict[str, Any]:
    """A `[resume] key` hook: the routed provider and a fixed tag."""
    return {"p": route["provider"], "tag": "v1"}
