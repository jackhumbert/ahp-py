"""In-process workers get the session's client tools (plan §8 M17.1)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import flow_fakes
from tests.clienttool_fakes import join_tool_client
from tests.flow_fakes import ClientToolProvider
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import run_turn, start_turn, state, wait_turn

TOML = (
    '[providers.ctool]\nload = "tests.flow_fakes:ClientToolProvider"\n\n'
    '[routing]\ndefault = "ctool"\n'
)
LOOKUP = {"lookup": "answer-42"}


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    flow_fakes.reset_fakes()
    yield
    flow_fakes.reset_fakes()


def write_workflow(tmp_path: Path, name: str, body: str) -> None:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    source = f"meta = {{'name': '{name}', 'description': 'd'}}\n\nasync def main(args):\n"
    source += "".join(f"    {line}\n" for line in body.splitlines())
    (user / f"{name}.py").write_text(source, encoding="utf-8")


async def joined(fx: FlowFixture, tools: dict[str, str]) -> None:
    await join_tool_client(fx.host, SESSION, tools)
    assert await fx.wire.until(lambda: bool(state(fx.host, SESSION).get("activeClients")), 10)
    # The host tells the agent right after publishing the list.
    await asyncio.sleep(0.1)


async def test_workerctool_call_answered(tmp_path: Path) -> None:
    write_workflow(tmp_path, "w", "return await agent('call:lookup')")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        await joined(fx, LOOKUP)
        turn = await run_turn(fx.wire, fx.chat, "t1", "/w")
        assert final_json(turn) == "lookup: answer-42"
        (session,) = ClientToolProvider.instances
        assert session.context.active_client_id == "editor"
    finally:
        await fx.shut()


async def test_workerctool_client_joining_mid_run(tmp_path: Path) -> None:
    write_workflow(tmp_path, "w", "return await agent('wait:lookup')")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/w")
        assert await fx.wire.until(lambda: len(ClientToolProvider.instances) == 1, 10)
        await joined(fx, LOOKUP)
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "lookup: answer-42"
        (session,) = ClientToolProvider.instances
        assert [c["clientId"] for c in session.clients] == ["editor"]
    finally:
        await fx.shut()


async def test_workerctool_disabled_hides(tmp_path: Path) -> None:
    write_workflow(tmp_path, "w", "return [await agent('list'), await agent('call:lookup')]")
    toml = TOML + "\n[workflows]\nclient_tools = false\n"
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), toml)
    try:
        await joined(fx, LOOKUP)
        turn = await run_turn(fx.wire, fx.chat, "t1", "/w")
        assert final_json(turn) == ["[]", "no client offers lookup"]
    finally:
        await fx.shut()


async def test_workerctool_narrowed_list(tmp_path: Path) -> None:
    write_workflow(tmp_path, "w", "return await agent('list')")
    toml = TOML + '\n[workflows]\nclient_tools = ["lookup"]\n'
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), toml)
    try:
        await joined(fx, {"lookup": "answer-42", "rename": "done"})
        turn = await run_turn(fx.wire, fx.chat, "t1", "/w")
        assert final_json(turn) == '["lookup"]'
    finally:
        await fx.shut()
