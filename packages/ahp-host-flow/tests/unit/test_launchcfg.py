"""`launch` / `per_call` in flow.toml providers, and `load_launcher`."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.config import ConfigError, FlowConfig, ProviderConfig, parse_config
from ahp_host_flow.loading import load_launcher

_LAUNCH = "tests.launch_cfg_target:launch"


def parse(provider: dict[str, Any], base: Path) -> FlowConfig:
    return parse_config(
        {"providers": {"a": provider}, "routing": {"default": "a"}},
        base=base,
    )


def spec(launch: str) -> ProviderConfig:
    return ProviderConfig("a", None, agent="claude", launch=launch)


def test_launchcfg_remote_with_launch(tmp_path: Path) -> None:
    a = parse({"launch": _LAUNCH, "agent": "claude"}, tmp_path).providers["a"]
    assert a.kind == "remote"
    assert a.launch == _LAUNCH
    assert a.url is None
    assert a.per_call is False


def test_launchcfg_per_call(tmp_path: Path) -> None:
    a = parse({"launch": _LAUNCH, "agent": "claude", "per_call": True}, tmp_path).providers["a"]
    assert a.per_call is True
    with pytest.raises(ConfigError, match=re.escape("providers.a.per_call needs launch")):
        parse({"url": "wss://x.example.com", "agent": "claude", "per_call": True}, tmp_path)
    with pytest.raises(ConfigError, match=re.escape("providers.a.per_call must be true or false")):
        parse({"launch": _LAUNCH, "agent": "claude", "per_call": "yes"}, tmp_path)


def test_launchcfg_exclusive(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError,
        match=re.escape("providers.a: load, via, url and launch are exclusive"),
    ):
        parse({"url": "wss://x.example.com", "launch": _LAUNCH, "agent": "claude"}, tmp_path)
    with pytest.raises(
        ConfigError,
        match=re.escape(
            "providers.a.token_file needs url (a launched host's transport comes from the launcher)"
        ),
    ):
        parse({"launch": _LAUNCH, "agent": "claude", "token_file": "tok"}, tmp_path)
    with pytest.raises(
        ConfigError,
        match=re.escape("providers.a.launch must be 'module:function', got 'nocolon'"),
    ):
        parse({"launch": "nocolon", "agent": "claude"}, tmp_path)


def test_launchcfg_load_launcher() -> None:
    import asyncio

    # typed as object: mypy refuses an identity check between the Launcher alias
    # and asyncio.sleep's overloaded type (comparison-overlap).
    loaded: object = load_launcher(spec("asyncio:sleep"))
    assert loaded is asyncio.sleep
    with pytest.raises(
        ConfigError,
        match=re.escape("providers.a.launch: 'math:pi' is not callable"),
    ):
        load_launcher(spec("math:pi"))
    with pytest.raises(ConfigError, match="nosuchmod"):
        load_launcher(spec("nosuchmod:x"))
