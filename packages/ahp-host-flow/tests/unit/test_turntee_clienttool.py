"""`TurnTee.run_client_tool`: the idle watchdog waits for the client, and only offered tools run."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ahp_host.provider.base import HOST_CLIENT_ID, ClientToolCall, ToolResult

from ahp_host_flow.bridge import TurnTee, Watchdog
from ahp_host_flow.clienttools import ClientTools

#: An editor offering `lint`, and the gateway offering the fleet tools.
ACTIVE = [
    {"clientId": "editor", "tools": [{"name": "lint"}]},
    {
        "clientId": "gw",
        "tools": [
            {"name": n}
            for n in ("start_session", "wait_for_sessions", "read_session", "stop_session")
        ],
    },
]


class _Sink:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[ClientToolCall] = []

    async def run_client_tool(self, call: ClientToolCall) -> ToolResult:
        self.calls.append(call)
        await asyncio.sleep(self.delay)
        return ToolResult(value={"success": True})


def _call(name: str, client_id: str = "editor") -> ClientToolCall:
    return ClientToolCall(call_id=f"c-{name}", name=name, client_id=client_id, tool_input={})


async def test_turntee_clienttool_pauses_the_idle_watchdog() -> None:
    expired: list[bool] = []
    watchdog = Watchdog(0.15, lambda: expired.append(True))
    watchdog.start()
    sink: Any = _Sink(delay=0.5)
    tee = TurnTee(sink, watchdog=watchdog, client_tools=ClientTools(True, ACTIVE))
    try:
        result = await tee.run_client_tool(_call("lint"))
        assert result.accepted
        assert expired == []
        # The clock restarts when the client answers, and runs again after that.
        await asyncio.sleep(0.3)
        assert expired == [True]
    finally:
        watchdog.stop()


async def test_turntee_clienttool_forwards_only_offered_tools() -> None:
    sink: Any = _Sink()
    tee = TurnTee(sink, client_tools=ClientTools(True, ACTIVE))
    assert (await tee.run_client_tool(_call("lint"))).accepted
    # The host's own tools for the turn (a structured result) are not a client's.
    assert (await tee.run_client_tool(_call("StructuredOutput", HOST_CLIENT_ID))).accepted
    for refused in (_call("deploy"), _call("start_session", "gw"), _call("lint", "gw")):
        with pytest.raises(LookupError):
            await tee.run_client_tool(refused)
    assert [c.name for c in sink.calls] == ["lint", "StructuredOutput"]


async def test_turntee_clienttool_without_client_tools_offers_none() -> None:
    sink: Any = _Sink()
    tee = TurnTee(sink)
    with pytest.raises(LookupError):
        await tee.run_client_tool(_call("lint"))
    assert sink.calls == []
