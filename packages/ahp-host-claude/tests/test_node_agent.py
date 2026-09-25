"""`type = "claude"` in an agent-host-node config."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
from agent_host_server.node import NodeContext, Roots

from agent_host_server_claude.agent import create


def test_the_package_registers_the_claude_agent_type() -> None:
    names = {entry.name: entry.value for entry in entry_points(group="agent_host_server.agents")}
    assert names.get("claude") == "agent_host_server_claude.agent:create"


async def test_an_unknown_option_is_refused_before_claude_starts(tmp_path: Path) -> None:
    node = NodeContext(roots=Roots.single(tmp_path), state_dir=tmp_path)
    with pytest.raises(ValueError, match="unknown option"):
        await create({"provider_id": "claude", "model": "opus"}, node)


async def test_a_bad_provider_id_is_refused(tmp_path: Path) -> None:
    node = NodeContext(roots=Roots.single(tmp_path), state_dir=tmp_path)
    with pytest.raises(ValueError, match="provider id"):
        await create({"provider_id": "has space"}, node)
