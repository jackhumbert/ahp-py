"""Remote workers advertise the run's client tools on their remote session (plan §8 M17.1c)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.clienttool_fakes import join_tool_client
from tests.flow_fakes import StrippedEchoProvider
from tests.flow_hosting import SESSION, FlowFixture, final_json
from tests.hosting import start_turn, state, wait_turn
from tests.integration.test_remoteapprove import TOML, booted


async def _joined(fx: FlowFixture) -> None:
    await join_tool_client(fx.host, SESSION, {"usages": "answer-42"})
    assert await fx.wire.until(
        lambda: bool(state(fx.host, SESSION).get("activeClients")), timeout=10
    )
    # The host tells the agent right after publishing the list.
    await asyncio.sleep(0.1)


async def test_remotectool_round_trip(tmp_path: Path) -> None:
    far = StrippedEchoProvider(provider_id="claude", client_tools=True)
    async with booted(tmp_path, far) as fx:
        await _joined(fx)
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        final = final_json(turn)
        assert isinstance(final, str)
        # Echo renders the wire result with str(), so the dict arrives in Python
        # repr form (single quotes), not JSON; measured from the failing run.
        assert final.startswith("The client's 'usages' tool said: ")
        assert "'text': 'answer-42'" in final


async def test_remotectool_disabled_advertises_none(tmp_path: Path) -> None:
    far = StrippedEchoProvider(provider_id="claude", client_tools=True)
    toml = TOML + "\n[workflows]\nclient_tools = false\n"
    async with booted(tmp_path, far, toml) as fx:
        await _joined(fx)
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "(no active client to run a tool)"
