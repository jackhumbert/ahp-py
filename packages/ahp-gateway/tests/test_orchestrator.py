"""The orchestrator: fleet tools for one session, run by the gateway's own links.

The nodes are real hosts (echo provider, with real folders), so everything the
orchestrator does to them is ordinary AHP a stock host accepts.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.core.resources import RootedFilesystemResourceProvider
from ahp_host.provider.echo import EchoProvider

from ahp_gateway.core import OrchestratorConfig
from ahp_gateway.core.orchestrator import FLEET_TOOLS
from ahp_gateway.registry import NodeRecord
from tests.fleet import DEV, Fleet, everyone_is_a_dev, generation, providers, reconnected

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _machine(base: Path, name: str, *, client_tools: bool = False) -> Host:
    root = base / name / "Github"
    (root / "project").mkdir(parents=True)
    return Host(
        EchoProvider(provider_id="claude", client_tools=client_tools),
        LoopbackSingleUserPolicy(),
        resources=RootedFilesystemResourceProvider(root),
        default_directory=root.resolve().as_uri(),
    )


def _fleet(tmp_path: Path, *names: str, client_tools: bool = False, **config: Any) -> Fleet:
    config.setdefault("child_config", {})
    return Fleet(
        {name: _machine(tmp_path, name, client_tools=client_tools) for name in names},
        [
            NodeRecord(name, f"mem://{name}", DEV, metadata={"label": name.title()})
            for name in names
        ],
        everyone_is_a_dev,
        orchestrator=OrchestratorConfig(**config),
    )


async def _eventually(condition: Callable[[], Any], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def _active_clients(host: Host, uri: str) -> list[Mapping[str, Any]]:
    state = host.sequencer.state_of(uri)
    clients = state.get("activeClients") if isinstance(state, Mapping) else None
    return list(clients or [])


async def _start_orchestrator(fleet: Fleet, uri: str = "orchestrator:/o1") -> None:
    async with fleet.surface() as surface:
        await surface.protocol.create_session(uri, provider="orchestrator")


async def _run(fleet: Fleet, uri: str, tool: str, **arguments: Any) -> dict[str, Any]:
    """Run one fleet tool as the node would ask for it, and return its result."""
    orchestrator = fleet.gateway.orchestrator
    assert orchestrator is not None
    adopted = orchestrator._adopted[uri]
    assert adopted.tools is not None
    _, executor = adopted.tools._tools[tool]
    assert executor is not None
    result: dict[str, Any] = await executor(
        {"turnId": "t1", "toolCallId": f"call-{tool}", "toolInput": json.dumps(arguments)}
    )
    return result


def _text(result: Mapping[str, Any]) -> str:
    return "\n".join(
        part["text"] for part in result.get("content") or [] if part.get("type") == "text"
    )


async def test_the_orchestrator_is_offered_when_its_agent_runs_somewhere(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, "studio")
    try:
        async with fleet.surface() as surface:
            assert providers(surface) == {"claude", "orchestrator"}
            [entry] = [a for a in surface.agents() if a["provider"] == "orchestrator"]
            assert entry["displayName"] == "Orchestrator"
    finally:
        await fleet.aclose()


async def test_not_offered_where_its_agent_does_not_run(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, "studio", provider="codex")
    try:
        async with fleet.surface() as surface:
            assert providers(surface) == {"claude"}
    finally:
        await fleet.aclose()


async def test_its_session_runs_the_real_agent_with_the_fleet_tools(tmp_path: Path) -> None:
    """Created by the gateway's own client on the surface's URI, which joins
    it offering every fleet tool - so the tools outlive the surface."""
    fleet = _fleet(tmp_path, "studio")
    try:
        await _start_orchestrator(fleet)
        host = fleet.hosts["studio"]
        state = host.sequencer.state_of("orchestrator:/o1")
        assert isinstance(state, Mapping)
        [client] = _active_clients(host, "orchestrator:/o1")
        assert client["clientId"].startswith("ahp-gateway-orchestrator-")
        assert [t["name"] for t in client["tools"]] == [t["name"] for t in FLEET_TOOLS]
    finally:
        await fleet.gateway.aclose()
        await fleet.aclose()


async def test_the_agent_calls_a_fleet_tool_and_gets_the_answer(tmp_path: Path) -> None:
    """End to end: the echo agent calls the first tool it was offered
    (`list_machines`), the gateway runs it, and the answer reaches the agent."""
    fleet = _fleet(tmp_path, "studio", "laptop", client_tools=True)
    try:
        async with fleet.surface() as surface:
            await surface.protocol.create_session("orchestrator:/o1", provider="orchestrator")
            session = await surface.open_session("orchestrator:/o1")
            chat = await session.chat()
            await chat.prompt("what do we have?", approvals="all")
            answer = json.dumps(chat.turns()[-1])
        assert "list_machines" in answer
        assert "laptop" in answer
        assert "studio" in answer
    finally:
        await fleet.gateway.aclose()
        await fleet.aclose()


async def test_list_machines_names_folders_and_agents(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, "studio", "laptop")
    try:
        await _start_orchestrator(fleet)
        machines = json.loads(_text(await _run(fleet, "orchestrator:/o1", "list_machines")))
        assert [m["machine"] for m in machines] == ["laptop", "studio"]
        assert machines[1]["name"] == "Studio"
        assert machines[0]["online"] is True, machines
        assert machines[0]["agents"] == ["claude"]
        assert [f["name"] for f in machines[0].get("folders", [])] == ["project"], machines
    finally:
        await fleet.gateway.aclose()
        await fleet.aclose()


async def test_a_worker_starts_where_asked_and_is_the_orchestrators_to_drive(
    tmp_path: Path,
) -> None:
    fleet = _fleet(tmp_path, "studio", "laptop")
    try:
        await _start_orchestrator(fleet)
        folder = (tmp_path / "laptop" / "Github" / "project").resolve()
        result = await _run(
            fleet,
            "orchestrator:/o1",
            "start_session",
            machine="laptop",
            prompt="fix the build",
            folder=str(folder),
            title="Build",
        )
        assert result["success"] is True
        [worker] = [p for p in result["content"] if p["type"] == "subagent"]
        assert worker["title"] == "Build"

        orchestrator = fleet.gateway.orchestrator
        assert orchestrator is not None
        [child] = orchestrator._adopted["orchestrator:/o1"].children.values()
        laptop = fleet.hosts["laptop"]
        state = laptop.sequencer.state_of(child.uri)
        assert isinstance(state, Mapping)
        assert state["workingDirectories"] == [folder.as_uri()]
        assert worker["resource"] == state["defaultChat"]
        # Workers get no fleet tools: a worker cannot start more workers.
        assert all(not c.get("tools") for c in _active_clients(laptop, child.uri))

        waited = json.loads(
            _text(await _run(fleet, "orchestrator:/o1", "wait_for_sessions", sessions=[child.uri]))
        )
        assert waited["timed_out"] is False
        assert waited["sessions"][0]["status"] == "done"
        read = _text(await _run(fleet, "orchestrator:/o1", "read_session", session=child.uri))
        assert "> fix the build" in read

        sent = await _run(
            fleet, "orchestrator:/o1", "send_message", session=child.uri, prompt="again"
        )
        assert sent["success"] is True
        listed = json.loads(_text(await _run(fleet, "orchestrator:/o1", "list_sessions")))
        assert [s["session"] for s in listed] == [child.uri]
    finally:
        await fleet.gateway.aclose()
        await fleet.aclose()


async def test_only_its_own_sessions(tmp_path: Path) -> None:
    """The user's other sessions are not the orchestrator's to message."""
    fleet = _fleet(tmp_path, "studio")
    try:
        async with fleet.surface() as surface:
            await surface.protocol.create_session("claude:/mine", provider="claude")
            await surface.protocol.create_session("orchestrator:/o1", provider="orchestrator")
        result = await _run(
            fleet, "orchestrator:/o1", "send_message", session="claude:/mine", prompt="hi"
        )
        assert result["success"] is False
        assert "not a session you started" in _text(result)
    finally:
        await fleet.gateway.aclose()
        await fleet.aclose()


async def test_workers_run_with_the_configured_mode_and_under_the_limit(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, "studio", max_running=0)
    try:
        await _start_orchestrator(fleet)
        result = await _run(
            fleet, "orchestrator:/o1", "start_session", machine="studio", prompt="go"
        )
        assert result["success"] is False
        assert "already running" in _text(result)

        refused = await _run(
            fleet,
            "orchestrator:/o1",
            "start_session",
            machine="studio",
            prompt="go",
            agent="orchestrator",
        )
        assert refused["success"] is False
    finally:
        await fleet.gateway.aclose()
        await fleet.aclose()


async def test_an_orchestrator_is_joined_again_after_a_restart(tmp_path: Path) -> None:
    """The state file names it, so a new gateway offers its tools again."""
    state_path = tmp_path / "orchestrators.json"
    fleet = _fleet(tmp_path, "studio", state_path=state_path)
    try:
        await _start_orchestrator(fleet)
        host = fleet.hosts["studio"]
        await fleet.gateway.aclose()
        await _eventually(lambda: _active_clients(host, "orchestrator:/o1") == [])

        restarted = Fleet(
            {"studio": host},
            [NodeRecord("studio", "mem://studio", DEV)],
            everyone_is_a_dev,
            orchestrator=OrchestratorConfig(state_path=state_path, child_config={}),
        )
        await restarted.gateway.start()
        await _eventually(lambda: _active_clients(host, "orchestrator:/o1") != [])
        [client] = _active_clients(host, "orchestrator:/o1")
        assert len(client["tools"]) == len(FLEET_TOOLS)
        await restarted.gateway.aclose()
    finally:
        await fleet.aclose()


async def test_its_tools_come_back_when_the_node_link_does(tmp_path: Path) -> None:
    """A host drops a client that disconnects from `activeClients`; the
    orchestrator's supervised link rejoins with its tools."""
    fleet = _fleet(tmp_path, "studio")
    try:
        await _start_orchestrator(fleet)
        host = fleet.hosts["studio"]
        orchestrator = fleet.gateway.orchestrator
        assert orchestrator is not None
        [client] = orchestrator._clients.values()
        past = generation(client)
        await fleet.connector.sever("studio")
        await reconnected(client, past)
        await _eventually(lambda: _active_clients(host, "orchestrator:/o1") != [])
        [joined] = _active_clients(host, "orchestrator:/o1")
        assert len(joined["tools"]) == len(FLEET_TOOLS)
    finally:
        await fleet.gateway.aclose()
        await fleet.aclose()
