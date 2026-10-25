"""`from_config`: a provider from a file and/or keywords, discovering on first use."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import AgentSessionContext, ModelInfo
from claude_agent_sdk import ClaudeAgentOptions

from ahp_host_claude import from_config
from ahp_host_claude import provider as provider_module
from ahp_host_claude.config import ConfigError
from tests.fakes import FakeClient


def _models(provider: Any) -> list[str]:
    return [m.id for m in provider.agent.models if isinstance(m, ModelInfo)]


def test_claude_from_config_reads_the_file(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    config = tmp_path / "claude.toml"
    config.write_text(
        'agent_name = "Claude here"\n'
        "port = 4321\n"
        'token_file = "t"\n'
        "verbose = true\n"
        f"[roots]\nwork = '{work}'\n",
        encoding="utf-8",
    )
    created: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        created.append(FakeClient(options, []))
        return created[-1]

    provider = from_config(config, state_dir=tmp_path / "s", client_factory=factory)
    assert provider.roots.names == ("work",)
    assert provider.agent.display_name == "Claude here"
    assert provider.agent.models == ()
    assert created == []


def test_claude_from_config_ignores_server_only_keys_in_the_file(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    config = tmp_path / "claude.toml"
    config.write_text(
        f"port = 4321\nbind = '0.0.0.0'\ntoken_file = 't'\nverbose = true\nroot = '{work}'\n",
        encoding="utf-8",
    )
    provider = from_config(config, state_dir=tmp_path / "s")
    assert provider.roots.paths == (work.resolve(),)


def test_claude_from_config_refuses_server_only_keywords(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    for key, value in (("port", 1), ("bind", "x"), ("token_file", "t"), ("verbose", True)):
        keyword: dict[str, Any] = {key: value}
        with pytest.raises(ValueError, match=f"unknown option.*{key}"):
            from_config(roots=[work], state_dir=tmp_path / "s", **keyword)


def test_claude_from_config_keywords_override_the_file(tmp_path: Path) -> None:
    work = tmp_path / "work"
    other = tmp_path / "other"
    work.mkdir()
    other.mkdir()
    config = tmp_path / "claude.toml"
    config.write_text(
        f"agent_name = \"File\"\n[roots]\nwork = '{work}'\n",
        encoding="utf-8",
    )
    provider = from_config(config, agent_name="Other", roots=[other], state_dir=tmp_path / "s")
    assert provider.agent.display_name == "Other"
    assert provider.roots.paths == (other.resolve(),)


def test_claude_from_config_from_keywords_only(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    provider = from_config(
        roots=[a, b],
        state_dir=tmp_path / "s",
        chat_tools=["WebSearch"],
        client_factory=lambda options: FakeClient(options, []),
    )
    assert provider.roots.paths == (a.resolve(), b.resolve())
    assert provider.chat_tools == ("WebSearch",)


async def test_claude_from_config_discovers_on_first_use(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    calls: list[int] = []

    class Discovering(FakeClient):
        async def get_server_info(self) -> dict[str, Any] | None:
            calls.append(1)
            return {
                "models": [
                    {
                        "value": "default",
                        "resolvedModel": "claude-opus-5-5",
                        "displayName": "Default",
                    },
                    {
                        "value": "haiku",
                        "resolvedModel": "claude-haiku-4-5",
                        "displayName": "Haiku",
                    },
                ],
                "commands": [],
            }

    provider = from_config(
        roots=[work],
        state_dir=tmp_path / "s",
        client_factory=lambda options: Discovering(options, []),
    )
    assert _models(provider) == []

    def context(n: int) -> AgentSessionContext:
        return AgentSessionContext(
            session_uri=f"s:/{n}",
            chat_uri=f"ahp-chat:/{n}",
            provider_id="claude",
            working_directories=(work.as_uri(),),
        )

    first = await provider.create_session(context(1))
    assert _models(provider) == ["default", "haiku"]
    second = await provider.create_session(context(2))
    assert len(calls) == 1
    await first.disposed()
    await second.disposed()
    await provider.aclose()


_INFO = {
    "models": [{"value": "default", "resolvedModel": "claude-opus-5-5", "displayName": "Default"}],
    "commands": [],
}


def _context(work: Path, n: int) -> AgentSessionContext:
    return AgentSessionContext(
        session_uri=f"s:/{n}",
        chat_uri=f"ahp-chat:/{n}",
        provider_id="claude",
        working_directories=(work.as_uri(),),
    )


class _Notifier:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return True


async def test_claude_from_config_retries_a_failed_discovery_on_the_next_session(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    asks: list[int] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, [])
        if not asks:  # the first client is the first discovery's probe, and it cannot start
            asks.append(1)
            client.connect_error = RuntimeError("not signed in")
        else:
            client.server_info = _INFO
        return client

    provider = from_config(roots=[work], state_dir=tmp_path / "s", client_factory=factory)
    first = await provider.create_session(_context(work, 1))
    assert _models(provider) == []
    second = await provider.create_session(_context(work, 2))
    assert _models(provider) == ["default"]
    third = await provider.create_session(_context(work, 3))
    assert _models(provider) == ["default"]
    for session in (first, second, third):
        await session.disposed()
    await provider.aclose()


async def test_claude_from_config_empty_discovery_is_retried_too(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    answers: list[dict[str, Any] | None] = [{"models": [], "commands": []}, _INFO]
    probes: list[int] = []

    class Answering(FakeClient):
        async def get_server_info(self) -> dict[str, Any] | None:
            probes.append(1)
            return answers[min(len(probes), len(answers)) - 1]

    provider = from_config(
        roots=[work],
        state_dir=tmp_path / "s",
        client_factory=lambda options: Answering(options, []),
    )
    first = await provider.create_session(_context(work, 1))
    assert _models(provider) == []
    second = await provider.create_session(_context(work, 2))
    assert _models(provider) == ["default"]
    assert len(probes) == 2
    await first.disposed()
    await second.disposed()
    await provider.aclose()


async def test_claude_from_config_attached_host_and_first_session_discover_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_module, "_REDISCOVERY", (0.0, 0.0))
    work = tmp_path / "work"
    work.mkdir()
    probes: list[int] = []

    class Slow(FakeClient):
        async def get_server_info(self) -> dict[str, Any] | None:
            probes.append(1)
            await asyncio.sleep(0.05)  # long enough for a racing rediscovery to start
            return _INFO

    provider = from_config(
        roots=[work], state_dir=tmp_path / "s", client_factory=lambda o: Slow(o, [])
    )
    changed = _Notifier()
    await provider.attach_agent_updates(changed)
    session = await provider.create_session(_context(work, 1))
    await asyncio.sleep(0.2)  # the rediscovery task, if it ran, has had its turn
    assert len(probes) == 1
    assert _models(provider) == ["default"]
    await session.disposed()
    await provider.aclose()


def test_claude_from_config_refusals(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match="unknown option"):
        from_config(roots=[work], state_dir=tmp_path / "s", model="opus")
    with pytest.raises(ConfigError, match="no folder to serve"):
        from_config(state_dir=tmp_path / "s")


def test_claude_from_config_reads_usage_per_message_from_the_file(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    config = tmp_path / "claude.toml"
    config.write_text(f"usage_per_message = true\nroot = '{work}'\n", encoding="utf-8")
    provider = from_config(config, state_dir=tmp_path / "s")
    assert provider.usage_per_message is True
    assert (
        from_config(config, usage_per_message=False, state_dir=tmp_path / "s").usage_per_message
        is False
    )


def test_usage_per_message_is_a_setting_of_the_standalone_server(tmp_path: Path) -> None:
    from ahp_host_claude.__main__ import _parse_args
    from ahp_host_claude.config import load

    work = tmp_path / "work"
    work.mkdir()
    config = tmp_path / "claude.toml"
    config.write_text(f"usage_per_message = true\nroot = '{work}'\n", encoding="utf-8")
    assert load(_parse_args(["--config", str(config)])).usage_per_message is True
    assert load(_parse_args(["--root", str(work)])).usage_per_message is False
    config.write_text(f"usage_per_message = 'yes'\nroot = '{work}'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="usage_per_message"):
        load(_parse_args(["--config", str(config)]))


def test_claude_from_config_applies_the_files_roots_layout_to_roots_keyword(
    tmp_path: Path,
) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    config = tmp_path / "claude.toml"
    config.write_text(f"roots_layout = 'real'\nroot = '{one}'\n", encoding="utf-8")
    provider = from_config(config, roots={"a": one, "b": two}, state_dir=tmp_path / "s")
    assert provider.roots.is_real
    plain = from_config(roots={"a": one, "b": two}, state_dir=tmp_path / "s2")
    assert not plain.roots.is_real
    config.write_text(f"roots_layout = 'flat'\nroot = '{one}'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="roots_layout"):
        from_config(config, roots={"a": one}, state_dir=tmp_path / "s3")
