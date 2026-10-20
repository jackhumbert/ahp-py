"""A provider with `per_call` gets a host launched and torn down for every call (plan §8 M17.2)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

from ahp_host_flow.provider import FlowSession
from tests import launch_fakes
from tests.clienttool_fakes import join_tool_client
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import (
    cancel_turn,
    flow_tasks,
    run_turn,
    start_turn,
    state,
    wait_turn,
    worker_chats,
)

TOML = (
    '[providers.box]\nlaunch = "tests.launch_fakes:launch"\nagent = "claude"\n'
    "per_call = true\n\n"
    '[routing]\ndefault = "box"\n'
)


@pytest.fixture(autouse=True)
def _launches() -> Iterator[None]:
    launch_fakes.reset()
    yield
    launch_fakes.reset()


def write_workflow(tmp_path: Path, name: str, body: str) -> None:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    source = f"meta = {{'name': '{name}', 'description': 'd'}}\n\nasync def main(args):\n"
    source += "".join(f"    {line}\n" for line in body.splitlines())
    (user / f"{name}.py").write_text(source, encoding="utf-8")


async def booted(tmp_path: Path, name: str, body: str) -> FlowFixture:
    write_workflow(tmp_path, name, body)
    return await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)


async def torn_down(fx: FlowFixture, count: int) -> None:
    assert await fx.wire.until(lambda: len(launch_fakes.LAUNCHES) == count, timeout=10)
    assert await fx.wire.until(
        lambda: all(item.closed and item.task.done() for item in launch_fakes.LAUNCHES), timeout=10
    )
    assert await fx.wire.until(lambda: not flow_tasks(), timeout=10)


async def test_percall_fan_out_tears_down_each_host(tmp_path: Path) -> None:
    fx = await booted(
        tmp_path,
        "fan",
        "return await parallel([\n"
        "    lambda: agent('a'),\n"
        "    lambda: agent('b'),\n"
        "    lambda: agent('c'),\n"
        "])",
    )
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/fan")
        results = final_json(turn)
        assert sorted(results) == [
            "echo: a cwd=- tools=[]",
            "echo: b cwd=- tools=[]",
            "echo: c cwd=- tools=[]",
        ]
        await torn_down(fx, 3)
        assert len({id(item.host) for item in launch_fakes.LAUNCHES}) == 3
    finally:
        await fx.shut()


async def test_percall_teardown_on_skip(tmp_path: Path) -> None:
    fx = await booted(tmp_path, "w", "return await agent('hang')")
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/w")
        assert await fx.wire.until(lambda: len(worker_chats(fx.host, SESSION)) == 1, timeout=10)
        worker = str(worker_chats(fx.host, SESSION)[0]["resource"])
        await fx.wire.request("subscribe", {"channel": worker})
        assert await fx.wire.until(
            lambda: (
                bool(launch_fakes.LAUNCHES) and state(fx.host, worker).get("activeTurn") is not None
            ),
            timeout=10,
        )
        await cancel_turn(fx.wire, worker, state(fx.host, worker)["activeTurn"]["id"])
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) is None
        await torn_down(fx, 1)
    finally:
        await fx.shut()


async def test_percall_teardown_on_abort(tmp_path: Path) -> None:
    fx = await booted(tmp_path, "w", "return await agent('hang')")
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/w")
        assert await fx.wire.until(lambda: len(launch_fakes.LAUNCHES) == 1, timeout=10)
        await cancel_turn(fx.wire, fx.chat, "t1")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert turn["state"] == "cancelled"
        await torn_down(fx, 1)
    finally:
        await fx.shut()


async def test_percall_teardown_on_failure(tmp_path: Path) -> None:
    fx = await booted(tmp_path, "w", "return await agent('fail')")
    try:
        await run_turn(fx.wire, fx.chat, "t1", "/w")
        await torn_down(fx, 1)
    finally:
        await fx.shut()


async def test_percall_passes_folder_and_tools(tmp_path: Path) -> None:
    fx = await booted(tmp_path, "w", "return await agent('x', x={'cwd': '/srv/proj'})")
    try:
        await join_tool_client(fx.host, SESSION, {"lookup": "answer-42"})
        # The host tells the flow session right after publishing the list: wait until the
        # session's client tools hold it, not for a fixed time.
        flow_session = fx.host._sessions[SESSION].agent_session
        assert isinstance(flow_session, FlowSession)
        assert await fx.wire.until(
            lambda: [t["name"] for t in flow_session._client_tools.definitions()] == ["lookup"],
            timeout=10,
        )
        turn = await run_turn(fx.wire, fx.chat, "t1", "/w")
        (launched,) = launch_fakes.LAUNCHES
        assert launched.request.folder == PurePosixPath("/srv/proj").as_uri()
        assert [t["name"] for t in launched.request.client_tools] == ["lookup"]
        assert final_json(turn) == "echo: x cwd=file:///srv/proj tools=['lookup']"
        await torn_down(fx, 1)
    finally:
        await fx.shut()
