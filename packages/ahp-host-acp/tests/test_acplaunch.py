"""AgentSpec's `launcher` and `env_allow` (plan M14 item 2), with a real subprocess."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import UserMessage

from ahp_host_acp.__main__ import _parse_args
from ahp_host_acp.agent import provider_from_options
from ahp_host_acp.config import ConfigError, Settings, load
from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec, launch_argv
from ahp_host_acp.roots import Roots

from .fakes import FAKE_AGENT, RecordingSink, context

FAKE_LAUNCHER = Path(__file__).with_name("fake_launcher.py")


async def _turn(session: AcpSession, text: str) -> str:
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text=text), sink)
    return "".join(str(e[1]) for e in sink.events if e[0] == "text")


async def _session(root: Path, spec: AgentSpec) -> AcpSession:
    return await AcpProvider(root, spec).create_session(context(root))


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("M14_DROP", "1")
    monkeypatch.setenv("M14_KEEP_ME", "1")
    monkeypatch.setenv("M14_KEEP_TOKEN", "s")
    monkeypatch.setenv("M14_EXACT_SECRET", "s")


async def test_acplaunch_env_allow_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    spec = AgentSpec(
        FAKE_AGENT,
        env={"FAKE_ACP_LOG": str(tmp_path / "log")},
        env_allow=("PATH", "SYSTEMROOT", "TEMP", "TMP", "M14_KEEP_*", "M14_EXACT_SECRET"),
    )
    session = await _session(tmp_path, spec)
    try:
        names = json.loads(await _turn(session, "environ"))["env"]
    finally:
        await session.aclose()
    folded = {name.casefold() for name in names}
    assert "path" in folded
    assert {"M14_KEEP_ME", "M14_EXACT_SECRET", "FAKE_ACP_LOG"} <= set(names)
    assert "M14_DROP" not in names
    assert "M14_KEEP_TOKEN" not in names


async def test_acplaunch_default_inherits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    session = await _session(tmp_path, AgentSpec(FAKE_AGENT))
    try:
        names = json.loads(await _turn(session, "environ"))["env"]
    finally:
        await session.aclose()
    assert "M14_DROP" in names
    assert "M14_KEEP_TOKEN" in names


async def test_acplaunch_launcher_prefix(tmp_path: Path) -> None:
    log = tmp_path / "launch.json"
    words = [sys.executable, str(FAKE_LAUNCHER), str(log), "{cwd}"]
    if sys.platform != "win32":
        words.append("{uid}:{gid}")
    session = await _session(tmp_path, AgentSpec(FAKE_AGENT, launcher=(*words, "--")))
    try:
        assert await _turn(session, "hello") == "Hi there"
        reply = json.loads(await _turn(session, "environ"))
    finally:
        await session.aclose()
    argv = json.loads(log.read_text(encoding="utf-8"))["argv"]
    assert argv[1] == str(tmp_path.resolve())
    if sys.platform != "win32":
        assert argv[2] == f"{os.getuid()}:{os.getgid()}"
    assert reply["argv"][0].endswith("fake_agent.py")


def test_acplaunch_config_keys(tmp_path: Path) -> None:
    provider = provider_from_options(
        {"command": ["x"], "launcher": ["docker", "run", "{cwd}"], "env_allow": ["PATH"]},
        Roots.single(tmp_path),
        tmp_path,
    )
    assert provider.spec.launcher == ("docker", "run", "{cwd}")
    assert provider.spec.env_allow == ("PATH",)

    def refused(options: dict[str, Any], message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            provider_from_options({"command": ["x"], **options}, Roots.single(tmp_path), tmp_path)

    refused({"launcher": "docker"}, "launcher must be a list of non-empty strings")
    refused({"env_allow": "PATH"}, "env_allow must be a list of variable names or glob patterns")

    config = tmp_path / "agent.toml"
    config.write_text(
        'command = ["x"]\nlauncher = ["docker", "run", "img"]\nenv_allow = ["PATH", "HOME"]\n',
        encoding="utf-8",
    )
    settings = load(_parse_args(["--config", str(config), "--root", str(tmp_path)]))
    assert isinstance(settings, Settings)
    assert settings.launcher == ("docker", "run", "img")
    assert settings.env_allow == ("PATH", "HOME")


def test_acplaunch_launch_argv() -> None:
    spec = AgentSpec(("agent", "acp"), launcher=("w", "-v", "{cwd}:{cwd}", "{x}"))
    path = Path("/p")
    assert launch_argv(spec, path) == ("w", "-v", f"{path}:{path}", "{x}", "agent", "acp")
