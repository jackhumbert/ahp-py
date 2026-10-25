"""Flow sessions are adopted by the gateway and get the engine-facing fleet tools.

All in memory: the nodes are stock hosts over memory transports.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ahp_gateway.registry import Principal
from tests.fleet import Fleet
from tests.flowfleet import (
    RecordingEcho,
    active_clients,
    eventually,
    flow_fleet,
    run_tool,
    start_flow,
)

pytestmark = pytest.mark.anyio

FLOW_TOOL_NAMES = ["start_session", "wait_for_sessions", "read_session", "stop_session"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _text(result: Mapping[str, Any]) -> str:
    return "\n".join(
        part["text"] for part in result.get("content") or [] if part.get("type") == "text"
    )


async def _close(fleet: Fleet) -> None:
    await fleet.gateway.aclose()
    await fleet.aclose()


def _recorder(fleet: Fleet, node: str) -> RecordingEcho:
    provider = fleet.hosts[node].provider
    assert isinstance(provider, RecordingEcho)
    return provider


async def test_fleetgw_tools_flow_session_is_joined_with_the_four_tools(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        [client] = active_clients(fleet.hosts["flow"], "flow:/r1")
        assert client["clientId"].startswith("ahp-gateway-orchestrator-")
        assert [t["name"] for t in client["tools"]] == FLOW_TOOL_NAMES
    finally:
        await _close(fleet)


async def test_fleetgw_tools_start_session_runs_on_the_node(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        result = await run_tool(
            fleet, "flow:/r1", "start_session", node="alpha", agent="claude", prompt="hi"
        )
        assert result["success"] is True
        started = result["structuredContent"]
        assert set(started) == {"session", "chat"}
        assert fleet.hosts["alpha"].sequencer.state_of(started["session"]) is not None
        assert ("alpha", "surface-1") in fleet.connector.dialed
        async with fleet.surface() as surface:
            listed = {item["resource"] for item in (await surface.sessions())["items"]}
        assert started["session"] in listed
    finally:
        await _close(fleet)


async def test_fleetgw_tools_start_session_passes_the_delegation_origin(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        named = await run_tool(
            fleet,
            "flow:/r1",
            "start_session",
            node="alpha",
            agent="claude",
            prompt="hi",
            originChat="ahp-chat:/w",
            originToolCall="agent-0",
        )
        plain = await run_tool(
            fleet, "flow:/r1", "start_session", node="alpha", agent="claude", prompt="hi"
        )
        half = await run_tool(
            fleet,
            "flow:/r1",
            "start_session",
            node="alpha",
            agent="claude",
            prompt="hi",
            originChat="ahp-chat:/w",
        )
        alpha = fleet.hosts["alpha"].sequencer
        state = alpha.state_of(named["structuredContent"]["session"])
        assert state["origin"] == {
            "kind": "ahp-py/delegation",
            "session": "flow:/r1",
            "chat": "ahp-chat:/w",
            "toolCallId": "agent-0",
        }
        for result in (plain, half):
            assert "origin" not in alpha.state_of(result["structuredContent"]["session"])
    finally:
        await _close(fleet)


async def test_fleetgw_tools_a_node_the_principal_is_not_admitted_to_is_refused(
    tmp_path: Path,
) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        result = await run_tool(
            fleet, "flow:/r1", "start_session", node="vault", agent="claude", prompt="hi"
        )
        assert result["success"] is False
        assert "no machine" in _text(result)
    finally:
        await _close(fleet)


async def test_fleetgw_tools_child_config_is_never_loosened(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path, child_config={"permissionMode": "default"})
    try:
        await start_flow(fleet)

        async def start(**extra: Any) -> dict[str, Any]:
            return await run_tool(
                fleet,
                "flow:/r1",
                "start_session",
                node="alpha",
                agent="claude",
                prompt="hi",
                **extra,
            )

        loose = await start(config={"permissionMode": "auto"})
        assert loose["success"] is False
        assert "looser" in _text(loose)

        strict = await start(config={"permissionMode": "plan"})
        assert strict["success"] is True
        assert _recorder(fleet, "alpha").configs[-1]["permissionMode"] == "plan"

        plain = await start()
        assert plain["success"] is True
        assert _recorder(fleet, "alpha").configs[-1]["permissionMode"] == "default"

        unknown = await start(config={"sandbox": 1})
        assert unknown["success"] is False
        assert "not allowed" in _text(unknown)
    finally:
        await _close(fleet)


async def test_fleetgw_tools_cannot_start_an_orchestrator_or_flow_session(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        for agent in ("flow", "orchestrator"):
            result = await run_tool(
                fleet, "flow:/r1", "start_session", node="alpha", agent=agent, prompt="hi"
            )
            assert result["success"] is False
            assert "cannot start an orchestrator or flow session" in _text(result)
    finally:
        await _close(fleet)


async def test_fleetgw_tools_stop_session_cancels_a_running_turn(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet)
        result = await run_tool(
            fleet, "flow:/r1", "start_session", node="asker", agent="claude", prompt="wait"
        )
        started = result["structuredContent"]
        host = fleet.hosts["asker"]

        def chat_state() -> Mapping[str, Any]:
            state = host.sequencer.state_of(started["chat"])
            return state if isinstance(state, Mapping) else {}

        await eventually(lambda: chat_state().get("activeTurn") is not None)
        stopped = await run_tool(fleet, "flow:/r1", "stop_session", session=started["session"])
        assert stopped["structuredContent"] == {"session": started["session"], "stopped": True}
        await eventually(
            lambda: (
                bool(chat_state().get("turns"))
                and chat_state()["turns"][-1].get("state") == "cancelled"
            )
        )
    finally:
        await _close(fleet)


async def test_fleetgw_tools_a_run_only_drives_the_sessions_it_started(tmp_path: Path) -> None:
    fleet = flow_fleet(tmp_path)
    try:
        await start_flow(fleet, "flow:/r1", "surface-1")
        await start_flow(fleet, "flow:/r2", "surface-2")
        started = (
            await run_tool(
                fleet, "flow:/r1", "start_session", node="alpha", agent="claude", prompt="hi"
            )
        )["structuredContent"]
        result = await run_tool(fleet, "flow:/r2", "stop_session", session=started["session"])
        assert result["success"] is False
        assert "not a session you started" in _text(result)
    finally:
        await _close(fleet)


async def test_fleetgw_tools_a_restarted_gateway_rejoins_the_flow_session(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    first = flow_fleet(tmp_path, state_path=state_path)
    await start_flow(first)
    started = (
        await run_tool(
            first, "flow:/r1", "start_session", node="alpha", agent="claude", prompt="hi"
        )
    )["structuredContent"]
    await first.gateway.aclose()

    second = flow_fleet(tmp_path, state_path=state_path, hosts=first.hosts)
    try:
        await second.gateway.start()

        def rejoined() -> bool:
            clients = active_clients(second.hosts["flow"], "flow:/r1")
            return [t["name"] for c in clients for t in c["tools"]] == FLOW_TOOL_NAMES

        await eventually(rejoined)
        await eventually(lambda: second.gateway.orchestrator._adopted["flow:/r1"].tools is not None)  # type: ignore[union-attr]
        result = await run_tool(second, "flow:/r1", "stop_session", session=started["session"])
        assert result["success"] is True
    finally:
        await _close(second)


async def test_fleetgw_tools_a_principal_who_loses_a_group_is_refused_on_its_next_flow(
    tmp_path: Path,
) -> None:
    """Admission is asked on every call, not only when a link is first dialed and cached."""
    groups = {"person": frozenset({"dev", "ops"})}
    fleet = flow_fleet(tmp_path)
    fleet.gateway.authenticate = lambda info: Principal(info.client_id, groups[info.client_id])
    try:
        await start_flow(fleet, "flow:/r1", "person")
        first = await run_tool(
            fleet, "flow:/r1", "start_session", node="vault", agent="claude", prompt="one"
        )
        assert first["success"] is True, first

        groups["person"] = frozenset({"dev"})
        await start_flow(fleet, "flow:/r2", "person")
        second = await run_tool(
            fleet, "flow:/r2", "start_session", node="vault", agent="claude", prompt="two"
        )
        assert second["success"] is False
        assert "no machine 'vault'" in _text(second)
    finally:
        await _close(fleet)
