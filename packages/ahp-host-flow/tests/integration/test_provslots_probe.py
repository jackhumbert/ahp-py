"""The concurrency probe: two trial turns tell a single-slot provider from a parallel one."""

from __future__ import annotations

from ahp_host.provider.base import AgentProvider, AgentSessionContext

from ahp_host_flow.limits import probe
from tests.limits_fakes import ParallelProvider, SingleSlotProvider


def _contexts() -> tuple[object, list[AgentSessionContext]]:
    made: list[AgentSessionContext] = []

    def factory() -> AgentSessionContext:
        context = AgentSessionContext(
            session_uri=f"flow-probe:/t/{len(made)}",
            chat_uri=f"flow-probe:/t/{len(made)}/chat",
            provider_id="t",
        )
        made.append(context)
        return context

    return factory, made


async def test_provslots_probe_single_slot() -> None:
    SingleSlotProvider.reset()
    logs: list[str] = []
    factory, made = _contexts()
    provider: AgentProvider = SingleSlotProvider()
    assert await probe(provider, factory, logs.append) == 1  # type: ignore[arg-type]
    assert logs == ["provider single-slot: probe found a single slot; concurrency 1"]
    assert len(made) == 2
    assert SingleSlotProvider.max_in_flight == 1


async def test_provslots_probe_parallel() -> None:
    ParallelProvider.reset()
    logs: list[str] = []
    factory, _ = _contexts()
    provider: AgentProvider = ParallelProvider()
    assert await probe(provider, factory, logs.append) is None  # type: ignore[arg-type]
    assert logs == ["provider parallel: probe found parallel turns; no limit"]
    assert ParallelProvider.max_in_flight == 2
