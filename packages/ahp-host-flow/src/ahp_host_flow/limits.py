"""Provider concurrency (plan §8 M16): `ProviderSlots`, the probe and the registry.

A provider's semaphore is shared by every run of the `FlowProvider`. A call holds it inside its
run's own Scheduler slot, never instead of it (`RoutingDispatcher`).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Final, cast

from ahp_host import TurnSink
from ahp_host.provider.base import AgentProvider, AgentSessionContext, UserMessage

from ahp_host_flow.config import FlowConfig

PROBE_PROMPT: Final = "Reply with the single word OK."
#: The second turn's first event, over the first turn's whole length, from which a provider is
#: taken to run one turn at a time: a single slot makes the second wait out the first (ratio ~1),
#: a parallel provider answers it at once (ratio ~ first event / total, near 0).
PROBE_RATIO: Final = 0.8

Log = Callable[[str], None]


class ProviderSlots:
    """How many calls of one provider may run at once, across every run."""

    def __init__(
        self,
        name: str,
        kind: str,
        limit: int | None = None,
        *,
        explicit: bool = False,
        setup: Callable[[], Awaitable[int | None]] | None = None,
    ) -> None:
        self.name = name
        self.kind = kind
        #: None: no limit.
        self.limit = limit
        #: A number from flow.toml; `on_busy` never changes it.
        self.explicit = explicit
        self.reason = "config" if explicit else "default"
        self.in_use = 0
        self._cond = asyncio.Condition()
        #: Runs once, under `_setup_lock`, before the first hold (the probe); its result is the
        #: limit.
        self._setup = setup
        self._setup_lock = asyncio.Lock()
        self._wakes: set[asyncio.Task[None]] = set()

    def set_limit(self, n: int | None, reason: str) -> None:
        self.limit = n
        self.reason = reason
        # A raised limit lets waiting calls in.
        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(self._wake())
            self._wakes.add(task)
            task.add_done_callback(self._wakes.discard)

    async def _wake(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def _ready(self) -> None:
        """Run the setup (the probe) once, before any call holds a slot.

        Every caller waits for it under the lock, and it is cleared only once it has answered:
        a first caller cancelled mid-probe leaves it in place, so the next caller probes again
        instead of finding no limit and no probe.
        """
        if self._setup is None:
            return
        async with self._setup_lock:
            setup = self._setup
            if setup is None:
                return
            limit = await setup()
            self._setup = None
            self.set_limit(limit, "probe")

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """Waits until fewer than `limit` calls are in use (at once when there is no limit)."""
        await self._ready()
        async with self._cond:
            await self._cond.wait_for(lambda: self.limit is None or self.in_use < self.limit)
            self.in_use += 1
        try:
            yield
        finally:
            self.in_use -= 1
            await asyncio.shield(self._wake())

    def on_busy(self, log: Log, kind: str = "rate_limited") -> None:
        """The provider said it is busy: halves a limit that is not from flow.toml (never to 0)."""
        if self.explicit:
            return
        old = self.limit
        new = max(1, (old if old is not None else self.in_use) // 2)
        if new == old:
            return
        shown = "unlimited" if old is None else old
        log(f"provider {self.name}: busy ({kind}); concurrency {shown} -> {new}")
        self.set_limit(new, f"busy ({kind})")


class _TimingSink:
    """A `TurnSink` that only notes when its first event came."""

    def __init__(self) -> None:
        self.first: float | None = None

    def __getattr__(self, name: str) -> Callable[..., Awaitable[None]]:
        if name.startswith("__"):
            raise AttributeError(name)

        async def record(*_args: Any, **_kwargs: Any) -> None:
            if self.first is None:
                self.first = time.monotonic()

        return record


async def _timed_turn(provider: AgentProvider, context: AgentSessionContext) -> tuple[float, float]:
    """(seconds to the first event, seconds to the end) of one probe turn; closes its session."""
    session = await provider.create_session(context)
    sink = _TimingSink()
    start = time.monotonic()
    try:
        await session.send_user_message(UserMessage(text=PROBE_PROMPT), cast(TurnSink, sink))
        total = time.monotonic() - start
        return (sink.first if sink.first is not None else start + total) - start, total
    finally:
        with contextlib.suppress(Exception):
            await session.aclose()


async def probe(
    provider: AgentProvider,
    context_factory: Callable[[], AgentSessionContext],
    log: Log,
    *,
    name: str | None = None,
) -> int | None:
    """Two minimal turns in parallel on two fresh sessions: 1 for a single-slot provider, else None.

    Each turn is timed from its send to its first sink event, and the first one to answer also
    to its end. A failure is logged and leaves None (no limit).
    """
    name = name if name is not None else provider.agent.provider
    results = await asyncio.gather(
        _timed_turn(provider, context_factory()),
        _timed_turn(provider, context_factory()),
        return_exceptions=True,
    )
    turns: list[tuple[float, float]] = []
    for result in results:
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            log(f"provider {name}: probe failed ({type(result).__name__}: {result}); no limit")
            return None
        turns.append(result)
    # The sessions started together, so the one answering first is "the first".
    turns.sort()
    (_, first_total), (second_first, _) = turns
    if second_first >= PROBE_RATIO * first_total:
        log(f"provider {name}: probe found a single slot; concurrency 1")
        return 1
    log(f"provider {name}: probe found parallel turns; no limit")
    return None


class ProviderSlotsRegistry:
    """One `ProviderSlots` per provider name, made on first use from config, shared by every
    session and run of a `FlowProvider`."""

    def __init__(
        self,
        config: FlowConfig,
        *,
        on_log: Log,
        prober: Callable[[str], Awaitable[int | None]] | None = None,
    ) -> None:
        self.config = config
        self.on_log = on_log
        #: Probes the provider of a name; `FlowProvider` supplies it.
        self._prober = prober
        self._slots: dict[str, ProviderSlots] = {}

    def for_name(self, name: str) -> ProviderSlots:
        slots = self._slots.get(name)
        if slots is not None:
            return slots
        spec = self.config.providers.get(name)
        concurrency = spec.concurrency if spec is not None else None
        kind = spec.kind if spec is not None else "unknown"
        if concurrency == "probe":
            prober = self._prober
            if prober is None:
                slots = ProviderSlots(name, kind)
            else:

                async def run_probe() -> int | None:
                    return await prober(name)

                slots = ProviderSlots(name, kind, setup=run_probe)
        elif isinstance(concurrency, int):
            slots = ProviderSlots(name, kind, concurrency, explicit=True)
        else:
            slots = ProviderSlots(name, kind)
        self._slots[name] = slots
        return slots
