"""Skip, abort and both restarts through an in-memory gateway (plan §8 M7)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ahp_client import Chat, Client, Session, TurnCompleted

from tests.fleet_hosting import (
    FleetFixture,
    active_count,
    boot_fleet,
    chat_state,
    final_json_of,
    restart_flow_host,
    restart_gateway,
    session_uris,
)
from tests.flow_hosting import final_json, store_for
from tests.hosting import notifications, state

RUN = "flow:/run-1"


def workflow(name: str, body: str) -> str:
    return f"meta = {{'name': {name!r}, 'description': 'd'}}\n\nasync def main(args):\n{body}"


WORKFLOWS = {
    "one": workflow("one", "    return await agent('one', label='A')\n"),
    "two": workflow(
        "two",
        "    return await parallel([\n"
        "        lambda: agent('x', label='A'),\n"
        "        lambda: agent('y', label='A'),\n"
        "    ])\n",
    ),
    "seq": workflow(
        "seq",
        "    a = await agent('one', label='A')\n"
        "    b = await agent('two', label='A')\n"
        "    return [a, b]\n",
    ),
    "fan": workflow(
        "fan",
        "    return await parallel([\n"
        "        lambda: agent('one', label='A'),\n"
        "        lambda: agent('two', label='B'),\n"
        "    ])\n",
    ),
}


async def boot(base: Path, **options: Any) -> FleetFixture:
    fx = await boot_fleet(base, **options)
    for name, source in WORKFLOWS.items():
        (base / "user" / f"{name}.py").write_text(source, encoding="utf-8")
    return fx


async def eventually(ready: Callable[[], bool]) -> None:
    async with asyncio.timeout(15):
        while not ready():
            await asyncio.sleep(0.01)


async def drive(chat: Chat, text: str) -> object:
    return await chat.prompt(text)


async def begin(surface: Client, text: str) -> tuple[Session, Chat, asyncio.Task[object]]:
    """Create the flow session and prompt its main chat without waiting for the end."""
    session = await surface.create_session(provider="flow", uri=RUN)
    chat = await session.chat()
    return session, chat, asyncio.create_task(drive(chat, text))


def last_turn_state(fx: FleetFixture, node: str, uri: str) -> object:
    turns = chat_state(fx.hosts[node], uri).get("turns", [])
    return turns[-1].get("state") if turns else None


async def test_fleet_e2e_restart_skip_stops_the_node_session(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    fx.alpha.gate.clear()
    try:
        async with fx.surface() as surface:
            session, _chat, run = await begin(surface, "/one")
            await eventually(lambda: active_count(fx.hosts["alpha"], "claude") == 1)
            await eventually(
                lambda: any((c.get("origin") or {}).get("kind") == "tool" for c in session.chats())
            )
            worker = next(
                c for c in session.chats() if (c.get("origin") or {}).get("kind") == "tool"
            )
            chat = await session.open_chat(str(worker["resource"]))
            await eventually(lambda: bool(chat.state.get("activeTurn")))
            await chat.cancel()
            async with asyncio.timeout(15):
                end = await run
            assert isinstance(end, TurnCompleted), end
            assert final_json_of(end) is None
            (uri,) = session_uris(fx.hosts["alpha"], "claude")
            await eventually(lambda: last_turn_state(fx, "alpha", uri) == "cancelled")
    finally:
        fx.alpha.gate.set()
        await fx.aclose()


async def test_fleet_e2e_restart_abort_stops_every_node_session(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    fx.alpha.gate.clear()
    try:
        async with fx.surface() as surface:
            _session, chat, run = await begin(surface, "/two")
            await eventually(lambda: active_count(fx.hosts["alpha"], "claude") == 2)
            await eventually(lambda: bool(chat.state.get("activeTurn")))
            await chat.cancel()
            async with asyncio.timeout(15):
                await run
            uris = session_uris(fx.hosts["alpha"], "claude")
            assert len(uris) == 2
            await eventually(
                lambda: all(last_turn_state(fx, "alpha", u) == "cancelled" for u in uris)
            )
    finally:
        fx.alpha.gate.set()
        await fx.aclose()


async def test_fleet_e2e_restart_flow_host_reattaches_to_the_node_session(tmp_path: Path) -> None:
    fx = await boot(tmp_path, flow_store=store_for(tmp_path))
    fx.alpha.gate.clear()
    run: asyncio.Task[object] | None = None
    try:
        async with fx.surface() as surface:
            _session, _chat, run = await begin(surface, "/seq")
            await eventually(lambda: active_count(fx.hosts["alpha"], "claude") == 1)
            host = await restart_flow_host(fx)
            fx.alpha.gate.set()

            def finished() -> bool:
                turns = chat_state(host, RUN).get("turns", [])
                return bool(turns) and turns[-1].get("state") == "complete"

            await eventually(finished)
            turn = chat_state(host, RUN)["turns"][-1]
            assert final_json(turn) == ["You said: one", "You said: two"]
            assert len(session_uris(fx.hosts["alpha"], "claude")) == 2
            assert any("reattached to node session" in note for note in notifications(turn))
    finally:
        fx.alpha.gate.set()
        if run is not None:
            run.cancel()
            with contextlib.suppress(BaseException):
                await run
        await fx.aclose()


def offers_fleet_tools(fx: FleetFixture) -> bool:
    clients = state(fx.hosts["flow"], RUN).get("activeClients") or []
    wanted = {"start_session", "wait_for_sessions", "read_session", "stop_session"}
    return any(wanted <= {t.get("name") for t in c.get("tools") or []} for c in clients)


async def test_fleet_e2e_restart_gateway_restart_rejoins_the_flow_session(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        async with fx.surface() as surface:
            await surface.create_session(provider="flow", uri=RUN)
        await eventually(lambda: offers_fleet_tools(fx))
        await restart_gateway(fx)
        await eventually(lambda: offers_fleet_tools(fx))
        async with fx.surface() as surface:
            session = await surface.open_session(RUN)
            chat = await session.chat()
            async with asyncio.timeout(15):
                end = await chat.prompt("/fan")
            assert isinstance(end, TurnCompleted), end
            assert final_json_of(end) == ["You said: one", "You said: two"]
    finally:
        await fx.aclose()


async def test_fleet_e2e_restart_a_run_rides_out_a_gateway_restart(tmp_path: Path) -> None:
    """Mid-call, the gateway restarts: the call is retried once on the rejoined gateway."""
    fx = await boot(tmp_path)
    fx.alpha.gate.clear()
    run: asyncio.Task[object] | None = None
    try:
        async with fx.surface() as surface:
            _session, _chat, run = await begin(surface, "/one")
            await eventually(lambda: active_count(fx.hosts["alpha"], "claude") == 1)
        await restart_gateway(fx)
        await eventually(lambda: offers_fleet_tools(fx))
        fx.alpha.gate.set()

        def finished() -> bool:
            turns = chat_state(fx.hosts["flow"], RUN).get("turns", [])
            return bool(turns) and turns[-1].get("state") == "complete"

        await eventually(finished)
        turn = chat_state(fx.hosts["flow"], RUN)["turns"][-1]
        assert final_json(turn) == "You said: one"
        # The node session the call started is the one it finished on.
        assert len(session_uris(fx.hosts["alpha"], "claude")) == 1
        assert any("trying once more" in note for note in notifications(turn))
    finally:
        fx.alpha.gate.set()
        if run is not None:
            run.cancel()
            with contextlib.suppress(BaseException):
                await run
        await fx.aclose()


async def test_fleet_e2e_restart_after_a_crash_a_client_resumes_the_run(tmp_path: Path) -> None:
    """A crash resumes nothing by itself; a surface's `chat/turnResume` does, reattached."""
    fx = await boot(tmp_path, flow_store=store_for(tmp_path))
    fx.alpha.gate.clear()
    run: asyncio.Task[object] | None = None
    try:
        async with fx.surface() as surface:
            _session, _chat, run = await begin(surface, "/seq")
            await eventually(lambda: active_count(fx.hosts["alpha"], "claude") == 1)
        host = await restart_flow_host(fx, drain=None)
        await eventually(lambda: offers_fleet_tools(fx))
        failed = chat_state(host, RUN)["turns"][-1]
        assert failed["state"] == "error"
        fx.alpha.gate.set()
        async with fx.surface() as surface:
            session = await surface.open_session(RUN)
            chat = await session.chat()
            surface.protocol.dispatch(chat.uri, {"type": "chat/turnResume", "turnId": failed["id"]})

            def finished() -> bool:
                turns = chat_state(host, RUN).get("turns", [])
                return bool(turns) and turns[-1].get("state") == "complete"

            await eventually(finished)
        turn = chat_state(host, RUN)["turns"][-1]
        assert final_json(turn) == ["You said: one", "You said: two"]
        assert len(session_uris(fx.hosts["alpha"], "claude")) == 2
        assert any("reattached to node session" in note for note in notifications(turn))
    finally:
        fx.alpha.gate.set()
        if run is not None:
            run.cancel()
            with contextlib.suppress(BaseException):
                await run
        await fx.aclose()
