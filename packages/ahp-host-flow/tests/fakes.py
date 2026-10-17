"""A scriptable fake `Dispatcher` for the engine's unit tests.

Not a test module. The prompt picks the behaviour: `hang` never returns,
`wait-for:<prompt>` waits until another call with that prompt has started,
`cancel` cancels its own turn, `fail` raises, `skip` returns null; anything
else echoes the prompt. `delays` adds a sleep for an exact prompt, after `hold`,
and `hang=False` makes "hang" prompts echo like any other.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from ahp_host_flow.engine import OUTPUT_TOKENS, AgentCall, AgentResult


class FakeDispatcher:
    def __init__(
        self,
        *,
        hold: float = 0.0,
        output_tokens: int = 0,
        delays: Mapping[str, float] | None = None,
        hang: bool = True,
    ) -> None:
        self._hold = hold
        self._delays = dict(delays or {})
        self._hang = hang
        self._output_tokens = output_tokens
        self.calls: list[AgentCall] = []
        self.in_flight = 0
        self.max_in_flight = 0
        # Ordinals whose dispatch got a CancelledError.
        self.cancelled: list[int] = []
        self._started: set[str] = set()
        self._cond = asyncio.Condition()

    async def wait_started(self, prompt: str) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: prompt in self._started)

    async def run_agent(self, call: AgentCall) -> AgentResult:
        self.calls.append(call)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        async with self._cond:
            self._started.add(call.prompt)
            self._cond.notify_all()
        usage = {OUTPUT_TOKENS: self._output_tokens} if self._output_tokens else {}
        try:
            prompt = call.prompt
            if self._hang and prompt.startswith("hang"):
                await asyncio.Event().wait()
            if prompt.startswith("wait-for:"):
                await self.wait_started(prompt[len("wait-for:") :])
            if self._hold > 0:
                await asyncio.sleep(self._hold)
            if (delay := self._delays.get(prompt)) is not None:
                await asyncio.sleep(delay)
            if prompt.startswith("cancel"):
                raise asyncio.CancelledError
            if prompt.startswith("fail"):
                raise RuntimeError("fake failure")
            if prompt.startswith("skip"):
                return AgentResult(value=None, usage=usage)
            return AgentResult(value=prompt, usage=usage)
        except asyncio.CancelledError:
            self.cancelled.append(call.ordinal)
            raise
        finally:
            self.in_flight -= 1
