"""Lazy provider loading by import path."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from ahp_host.node import Roots
from ahp_host.provider.base import AgentSessionContext
from ahp_host.provider.echo import EchoProvider

from ahp_host_flow.config import ConfigError, FlowConfig, ProviderConfig, Routing
from ahp_host_flow.loading import load_provider, load_providers
from tests.flow_fakes import RecordingProvider, RootedProvider, reset_fakes

ECHO = "ahp_host.provider.echo:EchoProvider"
REC = "tests.flow_fakes:RecordingProvider"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def test_load_without_config() -> None:
    assert isinstance(load_provider(ProviderConfig("e", ECHO)), EchoProvider)


async def test_load_with_table() -> None:
    provider = load_provider(ProviderConfig("e", ECHO, config={"structured": True}))
    session = await provider.create_session(
        AgentSessionContext(session_uri="s:/1", chat_uri="ahp-chat:/1", provider_id="echo")
    )
    assert session.offers_host_tools() is True  # type: ignore[attr-defined]


def test_load_with_path() -> None:
    provider = load_provider(ProviderConfig("r", REC, config=Path("x.toml")))
    assert isinstance(provider, RecordingProvider)
    assert provider.config_path == Path("x.toml")


def test_load_table_keywords() -> None:
    provider = load_provider(ProviderConfig("r", REC, config={"provider_id": "rec2"}))
    assert provider.agent.provider == "rec2"


@pytest.mark.parametrize(
    ("load", "match"),
    [
        ("tests.flow_nope:X", "cannot import"),
        ("tests.flow_fakes:Missing", "cannot import"),
        ("tests.flow_fakes:not_a_provider", "did not make an AgentProvider"),
    ],
)
def test_load_errors(load: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_provider(ProviderConfig("p", load))


def test_bad_config_is_config_error() -> None:
    with pytest.raises(ConfigError, match=r"providers."):
        load_provider(ProviderConfig("e", ECHO, config={"nonsense": 1}))


def test_load_providers_keeps_order() -> None:
    config = FlowConfig(
        providers={
            "b": ProviderConfig("b", REC, config={"provider_id": "b"}),
            "a": ProviderConfig("a", ECHO),
        },
        routing=Routing("a"),
        state_dir=Path("state"),
    )
    assert list(load_providers(config)) == ["b", "a"]


ROOTED = "tests.flow_fakes:RootedProvider"


def test_flow_from_config_loader_passes_roots_and_state_dir(tmp_path: Path) -> None:
    ws, st = tmp_path / "ws", tmp_path / "st"
    ws.mkdir()
    provider = load_provider(ProviderConfig("r", ROOTED), host_roots=Roots.single(ws), state_dir=st)
    assert isinstance(provider, RootedProvider)
    assert provider.roots.paths == (ws.resolve(), (st / "runs").resolve())
    assert provider.state_dir == st / "providers" / "r"
    assert (st / "runs").is_dir()
    assert (st / "providers" / "r").is_dir()


def test_flow_from_config_loader_names_the_runs_root(tmp_path: Path) -> None:
    a, st = tmp_path / "a", tmp_path / "st"
    a.mkdir()
    provider = load_provider(
        ProviderConfig("r", ROOTED), host_roots=Roots.named({"flow-runs": a}), state_dir=st
    )
    assert isinstance(provider, RootedProvider)
    assert provider.roots.names == ("flow-runs", "flow-runs-2")
    assert provider.roots.paths == (a.resolve(), (st / "runs").resolve())


def test_flow_from_config_loader_keeps_a_table_roots(tmp_path: Path) -> None:
    st = tmp_path / "st"
    provider = load_provider(ProviderConfig("r", ROOTED, config={"roots": "x"}), state_dir=st)
    assert isinstance(provider, RootedProvider)
    assert provider.roots == "x"
    assert provider.state_dir == st / "providers" / "r"


def test_flow_from_config_loader_skips_factories_without_them(tmp_path: Path) -> None:
    provider = load_provider(ProviderConfig("e", ECHO), state_dir=tmp_path / "st")
    assert isinstance(provider, EchoProvider)


def test_flow_from_config_loader_injects_nothing_without_state_dir() -> None:
    provider = load_provider(ProviderConfig("r", ROOTED))
    assert isinstance(provider, RootedProvider)
    assert provider.roots is None
    assert provider.state_dir is None
