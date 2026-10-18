"""`type = "flow"` in an ahp-node config."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
from ahp_host.node import NodeContext, Roots

from ahp_host_flow.agent import create
from ahp_host_flow.config import ConfigError


def _node(tmp_path: Path) -> NodeContext:
    return NodeContext(roots=Roots.named({"work": tmp_path}), state_dir=tmp_path / "node-state")


def _config(tmp_path: Path, extra: str = "") -> str:
    path = tmp_path / "flow.toml"
    table = "[providers.a]" + chr(10) + "load = 'pkg.mod:Cls'" + chr(10)
    path.write_text(extra + table, encoding="utf-8")
    return str(path)


def test_flow_node_agent_is_registered() -> None:
    names = {entry.name: entry.value for entry in entry_points(group="ahp_host.agents")}
    assert names.get("flow") == "ahp_host_flow.agent:create"


def test_flow_node_agent_defaults_state_to_the_nodes(tmp_path: Path) -> None:
    node = _node(tmp_path)
    provider = create({"config": _config(tmp_path)}, node)
    assert provider.config.state_dir == node.state_dir
    assert provider.agent.provider == "flow"


def test_flow_node_agent_keeps_a_configured_state_dir(tmp_path: Path) -> None:
    state = tmp_path / "mine"
    config = _config(tmp_path, f"state_dir = '{state.as_posix()}'\n")
    provider = create({"config": config}, _node(tmp_path))
    assert provider.config.state_dir == state


def test_flow_node_agent_refuses_an_unknown_option(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"flow agent: unknown option\(s\): port"):
        create({"config": _config(tmp_path), "port": 1}, _node(tmp_path))


def test_flow_node_agent_requires_a_config(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config"):
        create({}, _node(tmp_path))
