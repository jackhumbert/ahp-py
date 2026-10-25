"""`TurnTee.run_client_tool`: the idle watchdog waits for the client, and only offered tools run."""

from __future__ import annotations

import asyncio
from typing import Any

from ahp_host.provider.base import ClientToolCall, ToolResult

from ahp_host_flow.bridge import TurnTee, Watchdog


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
    tee = TurnTee(sink, watchdog=watchdog)
    try:
        result = await tee.run_client_tool(_call("lint"))
        assert result.accepted
        assert expired == []
        # The clock restarts when the client answers, and runs again after that.
        await asyncio.sleep(0.3)
        assert expired == [True]
    finally:
        watchdog.stop()
