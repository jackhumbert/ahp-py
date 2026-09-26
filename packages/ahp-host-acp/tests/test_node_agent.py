"""`type = "acp"` in an ahp-node config."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
from ahp_host.node import NodeContext, Roots
from ahp_host.provider.base import ModelInfo

from ahp_host_acp.agent import create
from ahp_host_acp.config import ConfigError


def _node(tmp_path: Path) -> NodeContext:
    return NodeContext(roots=Roots.named({"work": tmp_path}), state_dir=tmp_path)


def test_the_package_registers_the_acp_agent_type() -> None:
    names = {entry.name: entry.value for entry in entry_points(group="ahp_host.agents")}
    assert names.get("acp") == "ahp_host_acp.agent:create"


def test_an_agent_table_builds_a_provider_on_the_nodes_roots(tmp_path: Path) -> None:
    node = _node(tmp_path)
    provider = create(
        {
            "provider_id": "goose",
            "agent_name": "goose",
            "command": ["goose", "acp"],
            "env": {"GOOSE_MODE": "smart_approve"},
            "models": [{"id": "glm", "name": "GLM", "context_window": 1024, "vision": True}],
        },
        node,
    )
    assert provider.agent.provider == "goose"
    assert provider.agent.display_name == "goose"
    models = provider.agent.models
    assert [m.id for m in models if isinstance(m, ModelInfo)] == ["glm"]
    assert len(models) == 1
    assert provider.roots is node.roots


def test_an_unknown_option_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown option"):
        create({"command": ["goose", "acp"], "port": 4324}, _node(tmp_path))


def test_a_command_is_required(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no agent to run"):
        create({"provider_id": "goose"}, _node(tmp_path))


def test_a_bad_provider_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="provider id"):
        create({"provider_id": "has space", "command": ["x"]}, _node(tmp_path))
