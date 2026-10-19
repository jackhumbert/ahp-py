"""`opts.x.cwd` and `isolation` on fleet and remote providers (plan §8 M14)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from ahp_client import Client
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_protocol.transport import Transport, memory_pair

from ahp_host_flow.config import ProviderConfig
from ahp_host_flow.dispatch import FlowAgentError
from ahp_host_flow.engine.remote import RemoteAgentError, folder_uri
from ahp_host_flow.fleet import fleet_folder
from tests.fleet_fakes import FakeFleet, join_fake_gateway
from tests.flow_fakes import StrippedEchoProvider
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import all_channels, notifications, run_turn, state

FLEET_TOML = """
[providers.alpha]
via = "fleet"
node = "alpha"
agent = "claude"

[routing]
default = "alpha"
"""
REMOTE_TOML = """
[providers.far]
url = "wss://my-mac-mini.example.com"
agent = "claude"
"""


def workflow(name: str, body: str) -> str:
    return f"meta = {{'name': {name!r}, 'description': 'd'}}\n\nasync def main(args):\n{body}"


def write_workflows(tmp_path: Path, workflows: dict[str, str]) -> None:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    for name, source in workflows.items():
        (user / f"{name}.py").write_text(source, encoding="utf-8")


async def boot_fleet(
    tmp_path: Path, workflows: dict[str, str]
) -> tuple[FlowFixture, FakeFleet, Client]:
    write_workflows(tmp_path, workflows)
    fx = await boot_flow(tmp_path, "node", FLEET_TOML)
    fleet = FakeFleet()
    client = await join_fake_gateway(fx.host, SESSION, fleet)
    assert await fx.wire.until(lambda: bool(state(fx.host, SESSION).get("activeClients")))
    # The host hands the new client list to the session right after publishing it.
    await asyncio.sleep(0.1)
    return fx, fleet, client


async def test_callcwd_fleet_passes_folder(tmp_path: Path) -> None:
    fx, fleet, client = await boot_fleet(
        tmp_path,
        {
            "x": workflow("x", "    return await agent('x', x={'cwd': '/srv/proj/sub'})\n"),
            "y": workflow("y", "    return await agent('y', isolation='worktree')\n"),
            "rel": workflow("rel", "    return await agent('r', x={'cwd': 'proj/sub'})\n"),
            "uri": workflow("uri", "    return await agent('u', x={'cwd': 'file:///srv/p'})\n"),
        },
    )
    try:
        await run_turn(fx.wire, fx.chat, "t1", "/x")
        assert fleet.starts[0]["folder"] == "/srv/proj/sub"
        started = len(fleet.starts)
        turn = await run_turn(fx.wire, fx.chat, "t0", "/rel")
        assert final_json(turn) is None
        assert len(fleet.starts) == started  # refused before anything started
        assert any(
            "must be an absolute path or a file: URI for a fleet provider" in note
            for note in notifications(turn)
        )
        await run_turn(fx.wire, fx.chat, "t0b", "/uri")
        assert fleet.starts[-1]["folder"] == "file:///srv/p"
        turn = await run_turn(fx.wire, fx.chat, "t2", "/y")
        assert final_json(turn) == "alpha: y"
        assert "folder" not in fleet.starts[-1]
        assert any(
            "isolation 'worktree' is not applied on fleet provider 'alpha'; ignored" in note
            for note in notifications(turn)
        )
    finally:
        await client.aclose()
        await fx.shut()


async def test_callcwd_remote_create_session_gets_uri(tmp_path: Path) -> None:
    far_dir = tmp_path / "far"
    far_dir.mkdir()
    good = f"    return await agent('hello', x={{'cwd': {str(far_dir)!r}}})\n"
    write_workflows(
        tmp_path,
        {
            "good": workflow("good", good),
            "rel": workflow("rel", "    return await agent('hello', x={'cwd': 'sub'})\n"),
            "iso": workflow("iso", "    return await agent('hello', isolation='worktree')\n"),
        },
    )
    far = Host(
        StrippedEchoProvider(
            provider_id="claude", structured=True, capabilities={"multipleChats": {}}
        ),
        LoopbackSingleUserPolicy(),
    )
    serving: set[asyncio.Task[None]] = set()

    def dialer(spec: ProviderConfig) -> Callable[[], Awaitable[Transport]]:
        async def dial() -> Transport:
            client_end, server_end = memory_pair()
            task = asyncio.create_task(far.serve(server_end))
            serving.add(task)
            task.add_done_callback(serving.discard)
            return client_end

        return dial

    fx = await boot_flow(tmp_path, "node", REMOTE_TOML, provider_options={"remote_dialer": dialer})
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/good")
        assert final_json(turn) == "You said: hello"
        (uri,) = [u for u in all_channels(far) if "defaultChat" in state(far, u)]
        assert state(far, uri)["workingDirectories"] == [far_dir.as_uri()]
        turn = await run_turn(fx.wire, fx.chat, "t2", "/rel")
        assert final_json(turn) is None
        assert any(
            "must be an absolute path or a file: URI for a remote provider" in note
            for note in notifications(turn)
        )
        turn = await run_turn(fx.wire, fx.chat, "t3", "/iso")
        assert final_json(turn) == "You said: hello"
        assert any(
            "isolation 'worktree' is not applied on remote provider 'far'; ignored" in note
            for note in notifications(turn)
        )
    finally:
        await fx.shut()
        for task in list(serving):
            task.cancel()


def test_callcwd_fleet_folder() -> None:
    assert fleet_folder("/srv/a") == "/srv/a"
    assert fleet_folder("C:\\work\\a") == "C:\\work\\a"
    assert fleet_folder("file:///srv/a") == "file:///srv/a"
    for bad in ("sub", "./sub", "..", "", None, 3):
        with pytest.raises(FlowAgentError):
            fleet_folder(bad)


def test_callcwd_folder_uri() -> None:
    assert folder_uri({}) is None
    assert folder_uri({"x": {"cwd": "/srv/a b"}}) == "file:///srv/a%20b"
    assert folder_uri({"x": {"cwd": "C:\\w\\p"}}) == "file:///C:/w/p"
    assert folder_uri({"x": {"cwd": "file:///x"}}) == "file:///x"
    for bad in ("rel", ""):
        with pytest.raises(RemoteAgentError):
            folder_uri({"x": {"cwd": bad}})
