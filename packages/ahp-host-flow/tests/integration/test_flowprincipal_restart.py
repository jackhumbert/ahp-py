"""A resumed run keeps the principal it started with (M13), across a drain and a crash."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.flow_fakes import reset_fakes
from tests.flow_hosting import boot_flow, final_json, restart_flow, store_for
from tests.hook_fakes import PRINCIPAL_CALLS, HoldVerb, reset_hooks
from tests.hosting import start_turn
from tests.integration.test_flow_durability import resume_by_hand, wait_resumed
from tests.integration.test_flowext_hostgates import py
from tests.integration.test_flowprincipal import HeaderPolicy

TOML = (
    '[providers.hold]\nload = "tests.flow_fakes:RestartHoldProvider"\n'
    '\n[routing]\ndefault = "hold"\n'
    '\n[verbs]\nwho = "tests.hook_fakes:principal_hold_verb"\n'
)
WHO_PY = (
    "meta = {'name': 'whoflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await who()\n"
)


@pytest.mark.parametrize("drain", [True, False])
async def test_flowprincipal_survives_a_restart(tmp_path: Path, drain: bool) -> None:
    reset_fakes()
    reset_hooks()
    fx = await boot_flow(
        tmp_path,
        str(tmp_path / "no-node"),
        TOML,
        store=store_for(tmp_path),
        policy=HeaderPolicy(),
        headers={"x-forwarded-user": "alice"},
    )
    await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "whoflow", WHO_PY))
    assert await fx.wire.until(lambda: len(PRINCIPAL_CALLS) == 1)
    if drain:
        assert fx.chat in await fx.host.drain(0.2)
    await fx.shut()
    HoldVerb.holding = False
    # The new host has no policy hook: only the resume state can still name alice.
    fx2 = await restart_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        if not drain:
            await asyncio.sleep(0.3)
            await resume_by_hand(fx2)
        turn = await wait_resumed(fx2)
        assert PRINCIPAL_CALLS == ["alice", "alice"]
        assert final_json(turn) == "alice"
    finally:
        await fx2.shut()
