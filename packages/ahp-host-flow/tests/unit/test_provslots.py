"""Provider concurrency: a semaphore shared across runs, halved when the provider is busy."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ahp_host_flow.config import ConfigError, FlowConfig, ProviderConfig, Routing, parse_config
from ahp_host_flow.dispatch import RoutingDispatcher
from ahp_host_flow.engine import AgentCall, AgentResult, DispatchError
from ahp_host_flow.limits import ProviderSlots, ProviderSlotsRegistry


class Recording:
    """A fake inner dispatcher recording how many calls run at once."""

    def __init__(self, *, fail: DispatchError | None = None) -> None:
        self.fail = fail
        self.in_flight = 0
        self.max_in_flight = 0

    async def run_agent(self, call: AgentCall) -> AgentResult:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.02)
            if self.fail is not None:
                raise self.fail
            return AgentResult(call.prompt)
        finally:
            self.in_flight -= 1


def _config(tmp_path: Path, concurrency: int | None) -> FlowConfig:
    providers = {
        "fake": ProviderConfig(
            "fake", load="tests.flow_fakes:EchoProvider", concurrency=concurrency
        )
    }
    return FlowConfig(providers=providers, routing=Routing("fake"), state_dir=tmp_path)


async def test_provslots_serialises_two_runs(tmp_path: Path) -> None:
    config = _config(tmp_path, 1)
    registry = ProviderSlotsRegistry(config, on_log=lambda _text: None)
    inner = Recording()
    runs = [
        RoutingDispatcher(config, inprocess=inner, fleet=None, slots=registry) for _ in range(2)
    ]
    calls = [run.run_agent(AgentCall(ordinal=i, prompt=f"p{i}")) for run in runs for i in range(3)]
    results = await asyncio.gather(*calls)
    assert [r.value for r in results] == ["p0", "p1", "p2"] * 2
    assert inner.max_in_flight == 1
    assert registry.for_name("fake").in_use == 0


async def test_provslots_unlimited_runs_in_parallel(tmp_path: Path) -> None:
    config = _config(tmp_path, None)
    registry = ProviderSlotsRegistry(config, on_log=lambda _text: None)
    inner = Recording()
    run = RoutingDispatcher(config, inprocess=inner, slots=registry)
    await asyncio.gather(*(run.run_agent(AgentCall(ordinal=i, prompt="p")) for i in range(3)))
    assert inner.max_in_flight == 3


async def test_provslots_halves_on_busy() -> None:
    logs: list[str] = []
    slots = ProviderSlots("fake", "inprocess", 4)
    slots.on_busy(logs.append)
    assert slots.limit == 2
    slots.on_busy(logs.append)
    assert slots.limit == 1
    slots.on_busy(logs.append)
    assert slots.limit == 1
    assert logs == [
        "provider fake: busy (rate_limited); concurrency 4 -> 2",
        "provider fake: busy (rate_limited); concurrency 2 -> 1",
    ]
    explicit = ProviderSlots("fake", "inprocess", 4, explicit=True)
    explicit.on_busy(logs.append)
    assert explicit.limit == 4
    assert len(logs) == 2
    unlimited = ProviderSlots("fake", "inprocess")
    unlimited.on_busy(logs.append)
    assert unlimited.limit == 1
    assert logs[-1] == "provider fake: busy (rate_limited); concurrency unlimited -> 1"


async def test_provslots_halves_through_the_dispatcher(tmp_path: Path) -> None:
    config = _config(tmp_path, None)
    logs: list[str] = []
    registry = ProviderSlotsRegistry(config, on_log=logs.append)
    registry.for_name("fake").set_limit(4, "test")
    busy = Recording(fail=DispatchError("rate_limited", True, "429"))
    run = RoutingDispatcher(config, inprocess=busy, slots=registry)
    with pytest.raises(DispatchError):
        await run.run_agent(AgentCall(ordinal=0, prompt="p"))
    assert registry.for_name("fake").limit == 2
    # A permanent 'unavailable' is not a busy signal.
    other = Recording(fail=DispatchError("unavailable", False, "gone"))
    run = RoutingDispatcher(config, inprocess=other, slots=registry)
    with pytest.raises(DispatchError):
        await run.run_agent(AgentCall(ordinal=1, prompt="p"))
    assert registry.for_name("fake").limit == 2
    flaky = Recording(fail=DispatchError("unavailable", True, "down"))
    run = RoutingDispatcher(config, inprocess=flaky, slots=registry)
    with pytest.raises(DispatchError):
        await run.run_agent(AgentCall(ordinal=2, prompt="p"))
    assert registry.for_name("fake").limit == 1
    assert len(logs) == 2


def test_provslots_config(tmp_path: Path) -> None:
    def parse(**provider: object) -> ProviderConfig:
        data = {"providers": {"p": {"load": "pkg.mod:Cls", **provider}}}
        return parse_config(data, base=tmp_path).providers["p"]

    assert parse().concurrency is None
    assert parse(concurrency=3).concurrency == 3
    assert parse(concurrency="probe").concurrency == "probe"
    for bad in (0, -1, True, 1.5, "many"):
        with pytest.raises(ConfigError, match="concurrency"):
            parse(concurrency=bad)
    fleet = {"providers": {"f": {"via": "fleet", "node": "n", "agent": "a", "concurrency": 2}}}
    assert parse_config(fleet, base=tmp_path).providers["f"].concurrency == 2
    fleet["providers"]["f"]["concurrency"] = "probe"
    with pytest.raises(ConfigError, match="needs an in-process provider"):
        parse_config(fleet, base=tmp_path)


async def test_provslots_cancelled_first_caller_reruns_probe() -> None:
    calls = 0
    release = asyncio.Event()

    async def setup() -> int | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
        return 1

    slots = ProviderSlots("p", "inprocess", setup=setup)

    async def hold_once() -> None:
        async with slots.hold():
            pass

    first = asyncio.create_task(hold_once())
    await asyncio.sleep(0)
    while calls == 0:
        await asyncio.sleep(0)
    # A second caller waits for the probe instead of running with no limit.
    second = asyncio.create_task(hold_once())
    await asyncio.sleep(0.05)
    assert not second.done()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    # The probe the cancelled caller started is rerun by the next caller, not lost.
    await asyncio.wait_for(second, 2)
    assert calls == 2
    assert (slots.limit, slots.reason) == (1, "probe")
