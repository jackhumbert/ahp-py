"""The node: one config for the machine, its agents loaded through entry points."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ahp_host.node.config import ConfigError, load
from ahp_host.node.runner import create_agents


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _args(config: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "config": config,
        "root": None,
        "port": None,
        "bind": None,
        "token_file": None,
        "state_dir": None,
        "log_file": None,
        "verbose": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "work").mkdir(exist_ok=True)
    config = tmp_path / "node.toml"
    config.write_text(f"root = '{tmp_path / 'work'}'\nstate_dir = '{tmp_path / 'state'}'\n{body}")
    return config


def test_agents_are_read_in_order_with_their_own_options(tmp_path: Path) -> None:
    settings = load(
        _args(
            _write(
                tmp_path,
                """
[[agents]]
type = "claude"
[[agents]]
type = "acp"
provider_id = "opencode"
command = ["opencode", "acp"]
""",
            )
        )
    )
    assert [a.type for a in settings.agents] == ["claude", "acp"]
    assert settings.agents[1].options == {"provider_id": "opencode", "command": ["opencode", "acp"]}


def test_a_node_needs_an_agent(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no agents"):
        load(_args(_write(tmp_path, "")))


def test_two_agents_may_not_share_a_provider_id(tmp_path: Path) -> None:
    body = '[[agents]]\ntype = "echo"\n[[agents]]\ntype = "echo"\n'
    with pytest.raises(ConfigError, match="share the provider_id"):
        load(_args(_write(tmp_path, body)))


def test_unknown_settings_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown setting"):
        load(_args(_write(tmp_path, 'agent_name = "x"\n[[agents]]\ntype = "echo"\n')))


@pytest.mark.anyio
async def test_agents_are_built_by_their_packages(tmp_path: Path) -> None:
    body = """
[[agents]]
type = "echo"
provider_id = "claude"
agent_name = "Claude"
[[agents]]
type = "echo"
provider_id = "goose"
"""
    providers = await create_agents(load(_args(_write(tmp_path, body))))
    assert [p.agent.provider for p in providers] == ["claude", "goose"]
    assert providers[0].agent.display_name == "Claude"
    assert (tmp_path / "state" / "agents" / "goose").is_dir()


@pytest.mark.anyio
async def test_an_agent_type_no_package_provides_is_named(tmp_path: Path) -> None:
    settings = load(_args(_write(tmp_path, '[[agents]]\ntype = "nonesuch"\n')))
    with pytest.raises(ConfigError, match="nonesuch"):
        await create_agents(settings)


def test_log_file_and_tunnel_are_read(tmp_path: Path) -> None:
    body = """log_file = '~/node.log'
tunnel = ["ssh", "-N", "-R", "127.0.0.1:4402:127.0.0.1:4321", "ahp-tunnel@gateway"]
[[agents]]
type = "echo"
"""
    settings = load(_args(_write(tmp_path, body)))
    assert settings.log_file == Path("~/node.log").expanduser()
    assert settings.tunnel[:2] == ("ssh", "-N")


def test_a_tunnel_must_be_a_list_of_strings(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="tunnel"):
        load(_args(_write(tmp_path, 'tunnel = "ssh -N"\n[[agents]]\ntype = "echo"\n')))
