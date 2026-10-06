from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host_acp.__main__ import _parse_args
from ahp_host_acp.config import ConfigError, load


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "node.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_config_file_with_models_env_and_command(tmp_path: Path) -> None:
    root = tmp_path.as_posix()
    config = _write(
        tmp_path,
        f"""
agent_name = "OpenClaw"
provider_id = "openclaw"
command = ["openclaw", "acp"]
model_command = "/model {{model}} -s"
root = '{root}'

[env]
OPENCLAW_HIDE_BANNER = "1"

[config_options]
thought_level = "low"

[[models]]
id = "ollama/glm-5.3-flash:cloud"
name = "GLM 5.3 Flash"
context_window = 1048576
""",
    )
    settings = load(_parse_args(["--config", str(config)]))
    assert settings.command == ("openclaw", "acp")
    assert settings.env == {"OPENCLAW_HIDE_BANNER": "1"}
    assert settings.config_options == {"thought_level": "low"}
    assert settings.model_command == "/model {model} -s"
    assert settings.port == 4322
    (model,) = settings.models
    assert model.id == "ollama/glm-5.3-flash:cloud"
    assert model.max_context_window == 1048576


def test_flags_override_and_split_the_command(tmp_path: Path) -> None:
    settings = load(
        _parse_args(
            ["--root", str(tmp_path), "--command", "openclaw acp --verbose", "--model", "m1"]
        )
    )
    assert settings.command == ("openclaw", "acp", "--verbose")
    assert [m.id for m in settings.models] == ["m1"]


def test_a_command_is_required(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no agent to run"):
        load(_parse_args(["--root", str(tmp_path)]))


def test_unknown_settings_are_an_error(tmp_path: Path) -> None:
    config = _write(tmp_path, 'command = ["x"]\nroot = "."\nmodle = "typo"\n')
    with pytest.raises(ConfigError, match="modle"):
        load(_parse_args(["--config", str(config)]))


def test_unknown_model_settings_are_an_error(tmp_path: Path) -> None:
    config = _write(tmp_path, 'command = ["x"]\nroot = "."\n[[models]]\nid = "a"\nwindow = 3\n')
    with pytest.raises(ConfigError, match="window"):
        load(_parse_args(["--config", str(config)]))


def test_model_command_needs_the_placeholder(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="model_command"):
        load(
            _parse_args(["--root", str(tmp_path), "--command", "x", "--model-command", "/model -s"])
        )


def test_mcp_servers_and_boolean_options(tmp_path: Path) -> None:
    root = tmp_path.as_posix()
    config = _write(
        tmp_path,
        f"""
command = ["x"]
root = '{root}'

[config_options]
thought_level = "low"
auto_approve = false

[[mcp_servers]]
name = "files"
command = "mcp-server-filesystem /Users/me/projects"
[mcp_servers.env]
LOG = "1"

[[mcp_servers]]
name = "docs"
type = "http"
url = "https://example.com/mcp"
[mcp_servers.headers]
Authorization = "Bearer token"
""",
    )
    settings = load(_parse_args(["--config", str(config)]))
    assert settings.config_options == {"thought_level": "low", "auto_approve": False}
    files, docs = settings.mcp_servers
    assert (files.transport, files.command, files.env) == (
        "stdio",
        ("mcp-server-filesystem", "/Users/me/projects"),
        {"LOG": "1"},
    )
    assert (docs.transport, docs.url, docs.headers) == (
        "http",
        "https://example.com/mcp",
        {"Authorization": "Bearer token"},
    )


@pytest.mark.parametrize(
    ("server", "error"),
    [
        ('name = "a"', "needs a command"),
        ('command = ["a"]', "needs a name"),
        ('name = "a"\ntype = "ws"\nurl = "https://example.com"', "type must be"),
        ('name = "a"\ntype = "sse"', "needs an http"),
        ('name = "a"\ncommand = ["a"]\nurl = "https://example.com"', "url and headers"),
        ('name = "a"\ntype = "http"\nurl = "https://example.com"\ncommand = ["a"]', "command"),
        ('name = "a"\ncommand = ["a"]\nport = 3', "port"),
    ],
)
def test_bad_mcp_servers_are_an_error(tmp_path: Path, server: str, error: str) -> None:
    config = _write(tmp_path, f'command = ["x"]\nroot = "."\n[[mcp_servers]]\n{server}\n')
    with pytest.raises(ConfigError, match=error):
        load(_parse_args(["--config", str(config)]))


def test_mcp_server_names_are_unique(tmp_path: Path) -> None:
    server = '[[mcp_servers]]\nname = "a"\ncommand = ["a"]\n'
    config = _write(tmp_path, f'command = ["x"]\nroot = "."\n{server}{server}')
    with pytest.raises(ConfigError, match="duplicate"):
        load(_parse_args(["--config", str(config)]))
