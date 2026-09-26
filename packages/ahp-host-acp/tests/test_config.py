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
