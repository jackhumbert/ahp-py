"""`[quotas]` on the provider path: per-principal day totals that survive a restart (M16)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host import LoopbackSingleUserPolicy
from ahp_host.core.policies import principal_from_header

from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json, restart_flow, store_for
from tests.hosting import connect, start_turn, wait_turn

TOML = (
    '[providers.scripted]\nload = "tests.limits_fakes:ScriptedProvider"\n'
    "[quotas]\ntokens_per_day = 2\n"
)

WORKFLOW = (
    "meta = {'name': 'lim', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    try:\n"
    "        return await agent('one')\n"
    "    except Exception as exc:\n"
    "        return str(exc)\n"
)


class HeaderPolicy(LoopbackSingleUserPolicy):
    principal_of = staticmethod(principal_from_header("x-forwarded-user"))


async def _turn(fx: FlowFixture, user: str, turn_id: str) -> Any:
    """Run `/lim` as *user* on the fixture's chat."""
    wire, serving = await connect(fx.host, headers={"x-forwarded-user": user}, client_id=turn_id)
    try:
        await wire.request("subscribe", {"channel": SESSION})
        await wire.request("subscribe", {"channel": fx.chat})
        await start_turn(wire, fx.chat, turn_id, "/lim")
        return final_json(await wait_turn(wire, fx.chat, turn_id))
    finally:
        await wire.close()
        serving.cancel()


async def test_quota_hosted_per_principal(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir()
    (user / "lim.py").write_text(WORKFLOW, encoding="utf-8")
    node = str(tmp_path / "no-node")
    fx = await boot_flow(
        tmp_path,
        node,
        TOML,
        store=store_for(tmp_path),
        policy=HeaderPolicy(),
        headers={"x-forwarded-user": "alice"},
    )
    try:
        # One call spends 1 + 1 = 2 tokens: alice's whole day.
        assert await _turn(fx, "alice", "t1") == "ok: one"
        assert "quota tokens_per_day reached: 2 of 2" in await _turn(fx, "alice", "t2")
        assert await _turn(fx, "bob", "t3") == "ok: one"
    finally:
        await fx.shut()
    fx = await restart_flow(tmp_path, node, TOML, policy=HeaderPolicy())
    try:
        assert "quota tokens_per_day reached: 2 of 2" in await _turn(fx, "alice", "t4")
    finally:
        await fx.shut()
