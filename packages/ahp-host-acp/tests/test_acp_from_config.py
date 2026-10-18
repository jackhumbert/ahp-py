"""`ahp_host_acp.from_config`: a provider from Python, with no node and no subprocess."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import ModelInfo

from ahp_host_acp import AcpProvider, from_config
from ahp_host_acp.config import ConfigError


def _write(tmp_path: Path, extra: str = "") -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    path = tmp_path / "acp.toml"
    path.write_text(
        f"root = {json.dumps(str(work))}\n"
        'command = ["prog", "acp"]\n'
        'agent_name = "Goose"\n'
        "port = 4322\n"
        'token_file = "x.token"\n'
        f"{extra}"
        "[[models]]\n"
        'id = "glm"\n',
        encoding="utf-8",
    )
    return path


def _model_ids(provider: AcpProvider) -> list[str]:
    models = provider.agent.models
    return [m.id for m in models if isinstance(m, ModelInfo)]


def test_acp_from_config_reads_the_file(tmp_path: Path) -> None:
    provider = from_config(_write(tmp_path), state_dir=tmp_path / "state")
    assert provider.roots.paths == ((tmp_path / "work").resolve(),)
    assert provider.spec.command == ("prog", "acp")
    assert provider.agent.display_name == "Goose"
    assert _model_ids(provider) == ["glm"]


def test_acp_from_config_ignores_server_only_keys_in_the_file(tmp_path: Path) -> None:
    # `_write` puts `port` and `token_file` in the file; they are dropped, not refused.
    extra = "bind = '0.0.0.0'\nverbose = true\n"
    provider = from_config(_write(tmp_path, extra), state_dir=tmp_path / "s")
    assert provider.agent.display_name == "Goose"


def test_acp_from_config_refuses_server_only_keywords(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    for key, value in (("port", 1), ("bind", "x"), ("token_file", "t"), ("verbose", True)):
        keyword: dict[str, Any] = {key: value}
        with pytest.raises(ConfigError, match=f"unknown option.*{key}"):
            from_config(roots=work, state_dir=tmp_path / "s", command=["x"], **keyword)


def test_acp_from_config_keywords_override_the_file(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    provider = from_config(
        str(_write(tmp_path)),
        roots=[other],
        state_dir=tmp_path / "state",
        agent_name="Other",
        command=["x"],
    )
    assert provider.agent.display_name == "Other"
    assert provider.spec.command == ("x",)
    assert provider.roots.paths == (other.resolve(),)


def test_acp_from_config_from_keywords_only(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    provider = from_config(
        roots=[a, b], state_dir=tmp_path / "s", command=["x"], models=[{"id": "m"}]
    )
    assert provider.roots.paths == (a.resolve(), b.resolve())
    assert (tmp_path / "s").is_dir()
    named = from_config(roots={"work": a}, state_dir=tmp_path / "s", command=["x"])
    assert named.roots.names == ("work",)


def test_acp_from_config_refusals(tmp_path: Path) -> None:
    state = tmp_path / "state"
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ConfigError, match="unknown option"):
        from_config(roots=work, state_dir=state, command=["x"], port=1)
    bad = tmp_path / "bad.toml"
    bad.write_text("nonsense = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown setting"):
        from_config(bad, roots=work, state_dir=state)
    with pytest.raises(ConfigError, match="no folder to serve"):
        from_config(state_dir=state, command=["x"])
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="is not a directory"):
        from_config(roots=a_file, state_dir=state, command=["x"])
