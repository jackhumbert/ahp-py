"""A fleet run sees the gateway principal's subject through a verb (plan §8 M13)."""

from __future__ import annotations

from pathlib import Path

from ahp_client import TurnCompleted
from ahp_host import LoopbackSingleUserPolicy
from ahp_host.core.policies import principal_from_header

from tests.fleet_hosting import TOML, boot_fleet, final_json_of, start_run

HEADER = "x-forwarded-user"

WHO_PY = (
    "meta = {'name': 'whoflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await who()\n"
)


class HeaderPolicy(LoopbackSingleUserPolicy):
    principal_of = staticmethod(principal_from_header(HEADER))


async def test_flowprincipal_fleet_sees_the_gateway_subject(tmp_path: Path) -> None:
    fx = await boot_fleet(
        tmp_path,
        flow_toml_body=TOML + '\n[verbs]\nwho = "tests.hook_fakes:principal_verb"\n',
        flow_policy=HeaderPolicy(),
        principal_header=HEADER,
    )
    try:
        (tmp_path / "user" / "whoflow.py").write_text(WHO_PY, encoding="utf-8")
        async with fx.surface("surface-1") as surface:
            end = await start_run(surface, "flow:/run-who", "/whoflow")
            assert isinstance(end, TurnCompleted), end
            assert final_json_of(end) == "surface-1"
    finally:
        await fx.aclose()
