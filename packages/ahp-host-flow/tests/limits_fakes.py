"""Inner-provider fakes for the limits tests (M16); not a test module.

Loaded by import path (`tests.limits_fakes:<Name>`), as a flow.toml would.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import ClassVar

from ahp_host.provider.base import (
    AgentInfo,
    AgentSessionContext,
    ToolConfirmation,
    TurnSink,
    UserMessage,
)
from ahp_protocol.ext.usage import request_usage_meta

from ahp_host_flow.engine import AgentCall, AgentResult, usage_reporter
from ahp_host_flow.limits import PROBE_PROMPT
from tests.preamble import stripped


@dataclass
class ScriptedSession:
    """`fail:<error_type>[:resumable]` fails the turn; `usage:<in>:<out>:<cr>:<cost>` reports
    that usage; `requests:<k>:<out>:<gap>` reports k request-scoped usages of out tokens, gap
    seconds apart, then answers with a turn report of k*out; any other prompt is answered.

    The idle-watchdog prompts: `reason-only:<s>` streams reasoning every 0.05 s for s seconds and
    ends with no text; `reason-then-text:<s>` does the same and then replies 'done'; `silent`
    sleeps without a single event; `ask-then-text:<s>` asks for a confirmation (s is how long the
    test means to leave it open) and replies 'done' once answered. The retry prompts:
    `empty-then-text` replies '' the first time any session is sent it and 'second' afterwards
    (`reset()` clears the count); `always-empty` replies ''. These turns report 3 output tokens.
    `fail-on:<provider>:<error_type>` fails like `fail:` only in a session whose provider id is
    <provider>; any other provider's session replies 'ok' (the fallback tests).
    `spend-fail-on:<provider>:<error_type>:<out>` does the same after reporting <out> output tokens.
    `acp-cost:<add>` adds <add> USD to the session's running cost and reports it as ACP does.
    """

    #: Sends seen per stripped prompt, across every provider instance; the tests `reset()` it.
    sends: ClassVar[dict[str, int]] = {}

    context: AgentSessionContext
    messages: list[UserMessage] = field(default_factory=list)
    cancelled: list[str | None] = field(default_factory=list)
    closed: bool = False
    #: ACP's running session cost, for `acp-cost:<add>`.
    acp_total: float = 0.0

    @classmethod
    def reset(cls) -> None:
        cls.sends.clear()
        ScriptedProvider.instances.clear()

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        message = stripped(message)
        self.messages.append(message)
        prompt = message.text
        if prompt.startswith("fail:"):
            error_type, _, flag = prompt.removeprefix("fail:").partition(":")
            if flag == "resumable":
                await sink.turn_failed("API Error: 429 rate limit", error_type, resumable=True)
            else:
                await sink.turn_failed("scripted failure", error_type)
            return
        if prompt.startswith("acp-cost:"):
            self.acp_total += float(prompt.removeprefix("acp-cost:"))
            await sink.text_delta("ok")
            acp_cost = {"amount": self.acp_total, "currency": "USD"}
            await sink.usage(output_tokens=1, meta={"acpUsage": {"cost": acp_cost}})
            return
        if prompt.startswith("spend-fail-on:"):
            provider, error_type, out_tok = prompt.removeprefix("spend-fail-on:").split(":")
            if self.context.provider_id == provider:
                await sink.usage(output_tokens=int(out_tok))
                await sink.turn_failed("scripted failure", error_type)
            else:
                await sink.text_delta("ok")
                await sink.usage(input_tokens=1, output_tokens=1)
            return
        if prompt.startswith("fail-on:"):
            provider, _, error_type = prompt.removeprefix("fail-on:").partition(":")
            if self.context.provider_id == provider:
                await sink.turn_failed("scripted failure", error_type)
            else:
                await sink.text_delta("ok")
                await sink.usage(input_tokens=1, output_tokens=1)
            return
        if prompt.startswith("usage:"):
            in_tok, out_tok, cache, cost = prompt.removeprefix("usage:").split(":")
            await sink.text_delta("ok")
            await sink.usage(
                input_tokens=int(in_tok),
                output_tokens=int(out_tok),
                cache_read_tokens=int(cache),
                meta={"costUsd": float(cost)},
            )
            return
        if prompt.startswith("requests:"):
            count, out_tok, gap = prompt.removeprefix("requests:").split(":")
            for _ in range(int(count)):
                await sink.usage(output_tokens=int(out_tok), meta=request_usage_meta())
                await asyncio.sleep(float(gap))
                if self.cancelled:
                    return
            await sink.text_delta("ok")
            await sink.usage(output_tokens=int(count) * int(out_tok))
            return
        if prompt.startswith(("reason-only:", "reason-then-text:")):
            name, _, seconds = prompt.partition(":")
            loop = asyncio.get_running_loop()
            end = loop.time() + float(seconds)
            while loop.time() < end:
                await sink.reasoning_delta(".")
                await asyncio.sleep(0.05)
            if name == "reason-then-text":
                await sink.text_delta("done")
            await sink.usage(output_tokens=3)
            return
        if prompt == "silent":
            await asyncio.Event().wait()
        if prompt.startswith("ask-then-text:"):
            await sink.tool_call_started("ask-1", "edit", {"path": "b"})
            await sink.confirm_tool_call(
                ToolConfirmation(
                    call_id="ask-1",
                    name="edit",
                    invocation_message="Edit b",
                    tool_input={"path": "b"},
                )
            )
            await sink.text_delta("done")
            await sink.usage(output_tokens=3)
            return
        if prompt in ("empty-then-text", "always-empty"):
            seen = self.sends.get(prompt, 0)
            self.sends[prompt] = seen + 1
            if prompt == "empty-then-text" and seen > 0:
                await sink.text_delta("second")
            await sink.usage(output_tokens=3)
            return
        await sink.text_delta(f"ok: {prompt}")
        await sink.usage(input_tokens=1, output_tokens=1)

    async def cancel(self, reason: str | None = None) -> None:
        self.cancelled.append(reason)

    async def aclose(self) -> None:
        self.closed = True


class ScriptedProvider:
    """Sessions that fail or answer as their prompt says."""

    instances: ClassVar[list[ScriptedProvider]] = []

    def __init__(self, *, provider_id: str = "scripted") -> None:
        self.provider_id = provider_id
        self.sessions: list[ScriptedSession] = []
        ScriptedProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Scripted",
            description="Fails or answers as the prompt says.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> ScriptedSession:
        session = ScriptedSession(context)
        self.sessions.append(session)
        return session


class UsageDispatcher:
    """An engine `Dispatcher`: `use:<in>:<out>:<cr>:<cost>` returns that usage, `hang` never
    returns, `burn:<out>` reports that usage live and then hangs, `slow:<seconds>:<out>` sleeps
    and returns that usage, any other prompt echoes with no usage."""

    def __init__(self) -> None:
        self.calls: list[AgentCall] = []
        self.in_flight = 0
        self.max_in_flight = 0
        # Ordinals whose dispatch got a CancelledError.
        self.cancelled: list[int] = []

    async def run_agent(self, call: AgentCall) -> AgentResult:
        self.calls.append(call)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            prompt = call.prompt
            if prompt.startswith("hang"):
                await asyncio.Event().wait()
            if prompt.startswith("burn:"):
                usage_reporter()({"output_tokens": int(prompt.removeprefix("burn:"))})
                await asyncio.Event().wait()
            if prompt.startswith("slow:"):
                seconds, out_tok = prompt.removeprefix("slow:").split(":")
                await asyncio.sleep(float(seconds))
                return AgentResult(prompt, usage={"output_tokens": int(out_tok)})
            if prompt.startswith("use:"):
                in_tok, out_tok, cache, cost = prompt.removeprefix("use:").split(":")
                usage = {
                    "input_tokens": int(in_tok),
                    "output_tokens": int(out_tok),
                    "cache_read_tokens": int(cache),
                }
                return AgentResult(prompt, usage=usage, cost_usd=float(cost))
            return AgentResult(prompt)
        except asyncio.CancelledError:
            self.cancelled.append(call.ordinal)
            raise
        finally:
            self.in_flight -= 1


class _TurnCounter:
    """Counts the turns running at once across every session of one fake provider class."""

    in_flight: ClassVar[int] = 0
    max_in_flight: ClassVar[int] = 0

    @classmethod
    def reset(cls) -> None:
        cls.in_flight = 0
        cls.max_in_flight = 0

    @classmethod
    def enter(cls) -> None:
        cls.in_flight += 1
        cls.max_in_flight = max(cls.max_in_flight, cls.in_flight)

    @classmethod
    def leave(cls) -> None:
        cls.in_flight -= 1


class SingleSlotSession:
    """Every session shares one lock held for the whole turn: the turn sleeps 0.2 s inside it,
    then emits 'OK'."""

    def __init__(self, context: AgentSessionContext) -> None:
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        async with SingleSlotProvider.lock:
            SingleSlotProvider.enter()
            try:
                await asyncio.sleep(0.2)
                await sink.text_delta("OK")
            finally:
                SingleSlotProvider.leave()

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class SingleSlotProvider(_TurnCounter):
    """A provider that serves one turn at a time, however many sessions it has."""

    lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.lock = asyncio.Lock()

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider="single-slot",
            display_name="Single slot",
            description="One turn at a time.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> SingleSlotSession:
        return SingleSlotSession(context)


class ParallelSession:
    """Emits 'OK' at once, then sleeps 0.2 s."""

    def __init__(self, context: AgentSessionContext) -> None:
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        ParallelProvider.enter()
        try:
            await sink.text_delta("OK")
            await asyncio.sleep(0.2)
        finally:
            ParallelProvider.leave()

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class ParallelProvider(_TurnCounter):
    """A provider whose sessions run in parallel."""

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider="parallel",
            display_name="Parallel",
            description="Any number of turns at once.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> ParallelSession:
        return ParallelSession(context)


class ProbedSession:
    """The probe's prompt runs one at a time when `ProbedProvider.single`, else at once; any
    other prompt runs in parallel (so only the probed limit can serialise them), counted."""

    def __init__(self, context: AgentSessionContext) -> None:
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        if message.text == PROBE_PROMPT:
            ProbedProvider.probes += 1
            if ProbedProvider.single:
                async with ProbedProvider.lock:
                    await asyncio.sleep(0.2)
                    await sink.text_delta("OK")
            else:
                await sink.text_delta("OK")
                await asyncio.sleep(0.2)
            return
        ProbedProvider.enter()
        try:
            await asyncio.sleep(0.15)
            await sink.text_delta("ok")
        finally:
            ProbedProvider.leave()

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class ProbedProvider(_TurnCounter):
    """A provider for `concurrency = "probe"` end to end: single-slot or parallel to the probe."""

    single: ClassVar[bool] = True
    probes: ClassVar[int] = 0
    lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @classmethod
    def reset(cls, *, single: bool = True) -> None:
        super().reset()
        cls.single = single
        cls.probes = 0
        cls.lock = asyncio.Lock()

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider="probed", display_name="Probed", description="Probe fake.", models=()
        )

    async def create_session(self, context: AgentSessionContext) -> ProbedSession:
        return ProbedSession(context)
