"""`run_in_process` (`flow run --config`): nobody answers an inner session's questions."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ahp_host_flow.config import FlowConfig, parse_config
from ahp_host_flow.inprocess import FlowRunFailed, run_in_process

pytestmark = pytest.mark.node

ECHO = "ahp_host.provider.echo:EchoProvider"


def config(
    base: Path, node: str, *, confirm_tools: bool, approval_mode: str | None = None
) -> FlowConfig:
    provider: dict[str, object] = {"load": ECHO, "config": {"confirm_tools": confirm_tools}}
    if approval_mode is not None:
        provider["approval_mode"] = approval_mode
    return parse_config(
        {"state_dir": str(base / "state"), "node": node, "providers": {"echo": provider}},
        base=base,
    )


async def test_a_confirmation_nobody_can_answer_fails_the_run(
    tmp_path: Path, node_path: str
) -> None:
    cfg = config(tmp_path, node_path, confirm_tools=True)
    with pytest.raises(FlowRunFailed) as info:
        await asyncio.wait_for(
            run_in_process(cfg, "return await agent('hi')", root=tmp_path), timeout=30
        )
    message = str(info.value)
    assert "Echo Tool" in message
    assert "approval_mode" in message


async def test_a_run_with_nothing_to_ask_still_returns(tmp_path: Path, node_path: str) -> None:
    cfg = config(tmp_path, node_path, confirm_tools=False)
    value = await asyncio.wait_for(
        run_in_process(cfg, "return await agent('hi')", root=tmp_path), timeout=30
    )
    assert value == "You said: hi"
