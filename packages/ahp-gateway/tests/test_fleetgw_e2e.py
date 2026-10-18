"""The flow tools end to end: a real client tool call from the flow node's agent.

Every other flow-tool test calls the executors directly. Here the flow node's
agent (`ToolCaller`) makes the call through its turn's `run_client_tool`, so it
crosses the node, the gateway's link and the gateway's tool host and comes
back, as a workflow engine's does. Also: who may read or drive what a run
started, `max_running` across a gateway restart, and effort.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_client.client import actions

from tests.fleet import Fleet
from tests.flowfleet import (
    RecordingEcho,
    active_clients,
    call_through_the_node,
    eventually,
    flow_fleet,
    run_tool,
    start_flow,
)

pytestmark = pytest.mark.anyio

RUN = "flow:/r1"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _close(fleet: Fleet) -> None:
    await fleet.gateway.aclose()
    await fleet.aclose()


def _data(value: Mapping[str, Any]) -> dict[str, Any]:
    assert value.get("success") is True, value
    data = value.get("structuredContent")
    assert isinstance(data, dict), value
    return data


def _chat_state(fleet: Fleet, node: str, chat: str) -> Mapping[str, Any]:
    state = fleet.hosts[node].sequencer.state_of(chat)
    return state if isinstance(state, Mapping) else {}


def _sessions_on(fleet: Fleet, node: str) -> list[str]:
    states = fleet.hosts[node].sequencer._states
    return [u for u, s in states.items() if isinstance(s, Mapping) and "defaultChat" in s]


async def _joined(fleet: Fleet, uri: str = RUN) -> None:
    await eventually(lambda: bool(active_clients(fleet.hosts["flow"], uri)))


async def test_fleetgw_e2e_each_tool_through_a_real_client_tool_call(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        await _joined(fleet)
        async with fleet.surface() as surface:
            started = _data(
                await call_through_the_node(
                    surface, RUN, "start_session", node="alpha", agent="claude", prompt="hello"
                )
            )
            assert started["session"] in _sessions_on(fleet, "alpha")

            waited = _data(
                await call_through_the_node(
                    surface, RUN, "wait_for_sessions", sessions=[started["session"]]
                )
            )
            assert waited == {
                "sessions": [{"session": started["session"], "status": "done", "done": True}],
                "timed_out": False,
            }

            read = _data(
                await call_through_the_node(
                    surface, RUN, "read_session", session=started["session"]
                )
            )
            assert read["status"] == "done"
            assert "hello" in read["text"]

            held = _data(
                await call_through_the_node(
                    surface, RUN, "start_session", node="asker", agent="claude", prompt="wait"
                )
            )
            await eventually(
                lambda: _chat_state(fleet, "asker", held["chat"]).get("activeTurn") is not None
            )
            stopped = _data(
                await call_through_the_node(surface, RUN, "stop_session", session=held["session"])
            )
            assert stopped == {"session": held["session"], "stopped": True}
    finally:
        await _close(fleet)


async def test_fleetgw_e2e_wait_for_sessions_times_out(tmp_path: Path) -> None:
    """A session waiting on a person is not done: the wait ends at its timeout, `timed_out`."""
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        await _joined(fleet)
        async with fleet.surface() as surface:
            held = _data(
                await call_through_the_node(
                    surface, RUN, "start_session", node="asker", agent="claude", prompt="wait"
                )
            )
            await eventually(
                lambda: _chat_state(fleet, "asker", held["chat"]).get("activeTurn") is not None
            )
            loop = asyncio.get_running_loop()
            began = loop.time()
            waited = _data(
                await call_through_the_node(
                    surface, RUN, "wait_for_sessions", sessions=[held["session"]], timeout=0.3
                )
            )
            assert loop.time() - began >= 0.3
            assert waited["timed_out"] is True
            [row] = waited["sessions"]
            assert row["done"] is False
            assert row["status"] in ("waiting", "working")
    finally:
        await _close(fleet)


async def test_fleetgw_e2e_another_principal_cannot_read_or_drive_what_a_run_started(
    tmp_path: Path,
) -> None:
    """`surface-2` is another principal: it can neither open nor prompt a run's node session."""
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet, RUN, "surface-1")
        await _joined(fleet)
        async with fleet.surface("surface-1") as owner:
            started = _data(
                await call_through_the_node(
                    owner, RUN, "start_session", node="alpha", agent="claude", prompt="mine"
                )
            )
            # The owner may: the run's sessions are theirs.
            mine = await owner.open_session(started["session"])
            await mine.chat()
        turns_before = len(_chat_state(fleet, "alpha", started["chat"]).get("turns") or [])
        async with fleet.surface("surface-2") as other:
            with pytest.raises(Exception, match="another user's flow run"):
                await other.open_session(started["session"])
            with pytest.raises(Exception, match="another user's flow run"):
                await other.protocol.request("subscribe", {"channel": started["chat"]})
            other.protocol.dispatch(
                started["chat"], actions.turn_started("t-other", text="do something else")
            )
            await asyncio.sleep(0.3)
        assert len(_chat_state(fleet, "alpha", started["chat"]).get("turns") or []) == turns_before
        assert _chat_state(fleet, "alpha", started["chat"]).get("activeTurn") is None
    finally:
        await _close(fleet)


async def test_fleetgw_e2e_max_running_holds_across_a_gateway_restart(tmp_path: Path) -> None:
    """A restarted gateway has opened none of the run's children; a running one still counts."""
    state_path = tmp_path / "state.json"
    first = flow_fleet(tmp_path, state_path=state_path, max_running=1)
    await start_flow(first)
    await _joined(first)
    held = (await run_tool(first, RUN, "start_session", node="asker", agent="claude", prompt="w"))[
        "structuredContent"
    ]
    await first.gateway.aclose()

    second = flow_fleet(tmp_path, state_path=state_path, max_running=1, hosts=first.hosts)
    try:
        await second.gateway.start()
        orchestrator = second.gateway.orchestrator
        assert orchestrator is not None
        await eventually(lambda: orchestrator._adopted[RUN].tools is not None)
        before = set(_sessions_on(second, "alpha"))
        task = asyncio.create_task(
            run_tool(second, RUN, "start_session", node="alpha", agent="claude", prompt="next")
        )
        await asyncio.sleep(0.3)
        assert not task.done()
        # Waiting, not started: the node has no new session.
        assert set(_sessions_on(second, "alpha")) == before
        stopped = await run_tool(second, RUN, "stop_session", session=held["session"])
        assert stopped["success"] is True
        result = await asyncio.wait_for(task, 10)
        assert result["success"] is True
        assert result["structuredContent"]["session"] in _sessions_on(second, "alpha")
    finally:
        await _close(second)


def _recorder(fleet: Fleet, node: str) -> RecordingEcho:
    provider = fleet.hosts[node].provider
    assert isinstance(provider, RecordingEcho)
    return provider


async def test_fleetgw_e2e_effort_is_passed_unless_child_config_sets_it(tmp_path: Path) -> None:
    free = flow_fleet(tmp_path / "free")
    try:
        await start_flow(free)
        await _joined(free)
        async with free.surface() as surface:
            _data(
                await call_through_the_node(
                    surface,
                    RUN,
                    "start_session",
                    node="alpha",
                    agent="claude",
                    prompt="hi",
                    config={"effort": "high"},
                )
            )
            refused = await call_through_the_node(
                surface,
                RUN,
                "start_session",
                node="alpha",
                agent="claude",
                prompt="hi",
                config={"effort": "turbo"},
            )
            assert refused == {"success": False, "reason": "unknown effort 'turbo'"}
        assert _recorder(free, "alpha").configs[-1]["effort"] == "high"
    finally:
        await _close(free)

    fixed = flow_fleet(tmp_path / "fixed", child_config={"effort": "low"})
    try:
        await start_flow(fixed)
        await _joined(fixed)
        async with fixed.surface() as surface:
            _data(
                await call_through_the_node(
                    surface,
                    RUN,
                    "start_session",
                    node="alpha",
                    agent="claude",
                    prompt="hi",
                    config={"effort": "high"},
                )
            )
        # The gateway's own setting wins: the requested effort is dropped, not an error.
        assert _recorder(fixed, "alpha").configs[-1]["effort"] == "low"
    finally:
        await _close(fixed)
