"""`env_allow`: variables the SDK would hand Claude Code are blanked, not removed."""

from __future__ import annotations

from pathlib import Path

import pytest
from ahp_host.provider.base import AgentSessionContext

from ahp_host_claude.agent import from_config, parse_agent_options
from ahp_host_claude.provider import ENTRYPOINT, ClaudeSession


def _session(tmp_path: Path, **fields: object) -> ClaudeSession:
    context = AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude", config={})
    return ClaudeSession(context, root=tmp_path, client_factory=lambda o: None, **fields)  # type: ignore[arg-type,return-value]


def test_claudeenv_blanks_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("M14_DROP", "1")
    monkeypatch.setenv("M14_KEEP_ME", "1")
    monkeypatch.setenv("M14_KEEP_TOKEN", "s")
    session = _session(tmp_path, env_allow=("PATH", "M14_KEEP_*"))
    env = session._options().env
    assert env["M14_DROP"] == ""
    assert env["M14_KEEP_TOKEN"] == ""
    assert "M14_KEEP_ME" not in env
    assert not [key for key in env if key.casefold() == "path"]
    assert env["CLAUDE_CODE_ENTRYPOINT"] == ENTRYPOINT


def test_claudeenv_default_unchanged(tmp_path: Path) -> None:
    assert _session(tmp_path)._options().env == {"CLAUDE_CODE_ENTRYPOINT": ENTRYPOINT}


def test_claudeenv_options(tmp_path: Path) -> None:
    assert parse_agent_options({"env_allow": ["PATH"]}).env_allow == ("PATH",)
    with pytest.raises(ValueError, match="env_allow"):
        parse_agent_options({"env_allow": "PATH"})
    provider = from_config(roots=tmp_path, state_dir=tmp_path / "s", env_allow=["PATH"])
    assert provider.env_allow == ("PATH",)


def test_claudeenv_never_sets_claudecode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("M14_DROP", "1")
    for allow in (("PATH",), ("PATH", "CLAUDECODE"), ("*",)):
        env = _session(tmp_path, env_allow=allow)._options().env
        assert "CLAUDECODE" not in env, allow
    assert _session(tmp_path, env_allow=("PATH",))._options().env["M14_DROP"] == ""
