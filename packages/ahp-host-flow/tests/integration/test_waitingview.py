"""A step's `waiting` entries come from the session's `inputNeeded`, not the worker turn (M15)."""

from __future__ import annotations

from pathlib import Path

from ahp_protocol.ext.flow import run_view

from ahp_host_flow.dispatch import waiting_for
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, restart_flow, store_for
from tests.hosting import cancel_turn, start_turn, state, wait_turn, worker_chats
from tests.integration.test_flowext_hostgates import py

AUTH_TOML = '[providers.auth]\nload = "tests.flow_fakes:AuthProvider"\n'
ECHO_TOML = (
    "[providers.echo]\n"
    'load = "tests.flow_fakes:StrippedEchoProvider"\n'
    "config = { confirm_tools = true }\n"
)

ONE_PY = (
    "meta = {'name': 'oneflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('look it up')\n"
)
TWO_PY = (
    "meta = {'name': 'twoflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await parallel([lambda: agent('a', label='A'), lambda: agent('b', label='B')])\n"
)


def _awaiting(fx: FlowFixture) -> bool:
    steps = run_view(state(fx.host, fx.chat)).steps
    return bool(steps) and steps[0].awaiting


async def test_waitingview_mcp_authentication_sets_awaiting(tmp_path: Path) -> None:
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), AUTH_TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "oneflow", ONE_PY))
        assert await fx.wire.until(lambda: _awaiting(fx), 10.0)
        assert await fx.wire.until(lambda: bool(run_view(state(fx.host, fx.chat)).waiting), 10.0)
        entry = run_view(state(fx.host, fx.chat)).waiting[0]
        assert entry.kind == "toolAuthentication"
        assert entry.chat == worker_chats(fx.host, SESSION)[0]["resource"]
        await cancel_turn(fx.wire, fx.chat, "t1")
        await wait_turn(fx.wire, fx.chat, "t1")
    finally:
        await fx.shut()


async def test_waitingview_two_parallel_steps(tmp_path: Path) -> None:
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), ECHO_TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "twoflow", TWO_PY))
        assert await fx.wire.until(
            lambda: len(run_view(state(fx.host, fx.chat)).waiting) == 2, 10.0
        )
        waiting = run_view(state(fx.host, fx.chat)).waiting
        assert sorted(w.ordinal for w in waiting) == [0, 1]
        assert {w.kind for w in waiting} == {"toolConfirmation"}
        assert {w.chat for w in waiting} == {
            str(c["resource"]) for c in worker_chats(fx.host, SESSION)
        }
        await cancel_turn(fx.wire, fx.chat, "t1")
        await wait_turn(fx.wire, fx.chat, "t1")
    finally:
        await fx.shut()


def test_waitingview_waiting_for_filters() -> None:
    entries = [
        {"kind": "toolConfirmation", "chat": "c1"},
        {"kind": "toolConfirmation", "chat": "c2"},
        {"kind": "toolClientExecution", "chat": "c1"},
        {"kind": 3, "chat": "c1"},
        {"chat": "c1"},
        "junk",
        {"kind": "inputRequest", "chat": "c1"},
        {"kind": "toolConfirmation", "chat": "c1"},
    ]
    assert waiting_for(entries, "c1") == [
        {"kind": "toolConfirmation", "chat": "c1"},
        {"kind": "inputRequest", "chat": "c1"},
    ]
    assert waiting_for(None, "c1") == []


async def test_waitingview_restore_withdrawal_clears_it(tmp_path: Path) -> None:
    """A restart withdraws the open request; the resumed step waits on its new one only."""
    node = str(tmp_path / "no-node")
    fx = await boot_flow(tmp_path, node, ECHO_TOML, store=store_for(tmp_path))
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "oneflow", ONE_PY))
        assert await fx.wire.until(lambda: bool(run_view(state(fx.host, fx.chat)).waiting), 10.0)
        worker = str(worker_chats(fx.host, SESSION)[0]["resource"])
        (old,) = [e["id"] for e in state(fx.host, SESSION)["inputNeeded"] if e["chat"] == worker]
    finally:
        await fx.shut()  # a crash: the turn and its request are still open in storage
    fx2 = await restart_flow(tmp_path, node, ECHO_TOML)
    try:
        # The restore withdrew the request, and the step no longer shows it.
        assert all(e.get("id") != old for e in state(fx2.host, SESSION).get("inputNeeded") or [])
        assert run_view(state(fx2.host, fx2.chat)).waiting == ()
        await fx2.wire.dispatch(fx2.chat, {"type": "chat/turnResume", "turnId": "t1"})
        assert await fx2.wire.until(lambda: bool(run_view(state(fx2.host, fx2.chat)).waiting), 10.0)
        (entry,) = [
            e
            for e in state(fx2.host, SESSION)["inputNeeded"]
            if e.get("chat") != fx2.chat and e.get("kind") == "toolConfirmation"
        ]
        assert entry["id"] != old
        await fx2.wire.request("subscribe", {"channel": str(entry["chat"])})
        await fx2.wire.dispatch(
            str(entry["chat"]),
            {
                "type": "chat/toolCallConfirmed",
                "turnId": entry["turnId"],
                "toolCallId": entry["toolCall"]["toolCallId"],
                "approved": True,
                "confirmed": "user-action",
            },
        )
        assert await fx2.wire.until(lambda: run_view(state(fx2.host, fx2.chat)).waiting == ())
        await wait_turn(fx2.wire, fx2.chat, "t1")
    finally:
        await fx2.shut()
