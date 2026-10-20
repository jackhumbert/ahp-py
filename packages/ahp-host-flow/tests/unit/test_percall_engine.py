"""ClientDispatcher launches, dials, runs and tears down a host per call (plan §8 M17.2)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ahp_host_flow.engine import dispatcher as dispatcher_module
from ahp_host_flow.engine.dispatcher import AgentCall, DispatchHandle
from ahp_host_flow.engine.remote import ClientDispatcher, RemoteAgentError, RemoteSpec
from tests import launch_fakes


@pytest.fixture(autouse=True)
def _launches() -> Iterator[None]:
    launch_fakes.reset()
    yield
    launch_fakes.reset()


def _dispatcher(lines: list[str], launch: object = launch_fakes.launch) -> ClientDispatcher:
    spec = RemoteSpec("box", "claude", launch=launch, per_call=True)  # type: ignore[arg-type]
    return ClientDispatcher(spec, on_log=lines.append)


async def test_percall_engine_runs_and_tears_down() -> None:
    lines: list[str] = []
    result = await _dispatcher(lines).run_agent(AgentCall(0, "hi"))
    assert result.value == "echo: hi cwd=- tools=[]"
    (launched,) = launch_fakes.LAUNCHES
    assert launched.closed
    assert launched.task.done()


async def test_percall_engine_reattach_runs_again_logged() -> None:
    lines: list[str] = []
    token = dispatcher_module._REATTACH.set(DispatchHandle(provider="box", session="claude:/old"))
    try:
        result = await _dispatcher(lines).run_agent(AgentCall(3, "hi"))
    finally:
        dispatcher_module._REATTACH.reset(token)
    assert lines == [
        "agent() #3: provider 'box' launches a host per call; the one the restart cut off is "
        "gone, so the call runs again"
    ]
    assert result.value == "echo: hi cwd=- tools=[]"
    assert len(launch_fakes.LAUNCHES) == 1


async def test_percall_engine_launch_failure_unavailable() -> None:
    dispatcher = _dispatcher([], launch_fakes.broken_launch)
    with pytest.raises(RemoteAgentError) as caught:
        await dispatcher.run_agent(AgentCall(0, "hi"))
    assert caught.value.kind == "unavailable"
    assert caught.value.transient is True
    assert "no container runtime" in str(caught.value)


async def test_percall_engine_failure_closes() -> None:
    with pytest.raises(RemoteAgentError):
        await _dispatcher([]).run_agent(AgentCall(0, "fail"))
    (launched,) = launch_fakes.LAUNCHES
    assert launched.closed
    assert launched.task.done()
