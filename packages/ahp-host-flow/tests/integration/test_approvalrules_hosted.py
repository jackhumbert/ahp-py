"""Approval rules and the flow policy answer a worker's confirmations (plan §8 M15)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import notifications, start_turn, state, wait_turn
from tests.integration.test_flowext_hostgates import py
from tests.integration.test_flowprincipal import HeaderPolicy

PROVIDER = '[providers.hinted]\nload = "tests.flow_fakes:HintedProvider"\n\n'


def _workflow(calls: list[dict[str, Any]]) -> str:
    return (
        "meta = {'name': 'hinted', 'description': 'd'}\n"
        "\n"
        "async def main(args):\n"
        f"    return await agent({json.dumps(calls)!r}, label='hinted')\n"
    )


def _worker_entries(fx: FlowFixture) -> list[Any]:
    needed = state(fx.host, SESSION).get("inputNeeded") or []
    return [e for e in needed if isinstance(e, dict) and e.get("chat") != fx.chat]


async def _approve(fx: FlowFixture, entry: dict[str, Any]) -> None:
    await fx.wire.request("subscribe", {"channel": str(entry["chat"])})
    await fx.wire.dispatch(
        str(entry["chat"]),
        {
            "type": "chat/toolCallConfirmed",
            "turnId": entry["turnId"],
            "toolCallId": entry["toolCall"]["toolCallId"],
            "approved": True,
            "confirmed": "user-action",
        },
    )


async def test_approvalrules_read_only_auto_approved_write_prompts(tmp_path: Path) -> None:
    toml = PROVIDER + "[[approvals.rules]]\nread_only = true\naction = 'allow'\n"
    calls = [
        {"name": "Read", "input": {"file_path": "a"}, "read_only": True},
        {"name": "Write", "input": {"file_path": "a"}, "read_only": False},
    ]
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), toml)
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "hinted", _workflow(calls)))
        assert await fx.wire.until(lambda: len(_worker_entries(fx)) == 1), state(fx.host, SESSION)
        entry = _worker_entries(fx)[0]
        assert entry["toolCall"]["toolName"] == "Write"
        await _approve(fx, entry)
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "Read=yes Write=yes"
        assert any("Read approved by rule 1" in n for n in notifications(turn))
    finally:
        await fx.shut()


async def test_approvalrules_rule_cannot_override_policy_deny(tmp_path: Path) -> None:
    toml = (
        PROVIDER
        + "[policy]\ndeny_commands = ['git push*']\n\n"
        + "[[approvals.rules]]\ntool = '*'\naction = 'allow'\n"
    )
    calls = [{"name": "Bash", "input": {"command": "git push"}, "read_only": None}]
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), toml)
    seen: list[Any] = []

    async def watch() -> None:
        while True:
            seen.extend(_worker_entries(fx))
            await asyncio.sleep(0.005)

    watcher = asyncio.ensure_future(watch())
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "hinted", _workflow(calls)))
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "Bash=no(denied by policy: the command matches 'git push*')"
        assert seen == []
    finally:
        watcher.cancel()
        await fx.shut()


async def test_approvalrules_person_rule_hosted(tmp_path: Path) -> None:
    toml = PROVIDER + "[[approvals.rules]]\ntool = 'Bash'\npersons = ['alice']\naction = 'allow'\n"
    calls = [{"name": "Bash", "input": {"command": "ls"}, "read_only": None}]
    alice = await boot_flow(
        tmp_path,
        str(tmp_path / "no-node"),
        toml,
        policy=HeaderPolicy(),
        headers={"x-forwarded-user": "alice"},
    )
    try:
        await start_turn(alice.wire, alice.chat, "t1", py(tmp_path, "hinted", _workflow(calls)))
        turn = await wait_turn(alice.wire, alice.chat, "t1")
        assert final_json(turn) == "Bash=yes"
        assert _worker_entries(alice) == []
    finally:
        await alice.shut()
    bob = await boot_flow(
        tmp_path,
        str(tmp_path / "no-node"),
        toml,
        policy=HeaderPolicy(),
        headers={"x-forwarded-user": "bob"},
    )
    try:
        await start_turn(bob.wire, bob.chat, "t1", py(tmp_path, "hinted", _workflow(calls)))
        assert await bob.wire.until(lambda: len(_worker_entries(bob)) == 1), state(
            bob.host, SESSION
        )
        await _approve(bob, _worker_entries(bob)[0])
        turn = await wait_turn(bob.wire, bob.chat, "t1")
        assert final_json(turn) == "Bash=yes"
    finally:
        await bob.shut()
