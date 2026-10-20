"""`fallback` in flow.toml and the chain `fallback_chain` walks (plan §8 M16)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.config import ConfigError, FlowConfig, parse_config
from ahp_host_flow.engine import AgentCall
from ahp_host_flow.routing import fallback_chain


def parse(providers: dict[str, Any], rules: list[dict[str, Any]] | None = None) -> FlowConfig:
    data: dict[str, Any] = {"providers": providers}
    routing: dict[str, Any] = {"default": next(iter(providers))}
    if rules is not None:
        routing["rules"] = rules
    data["routing"] = routing
    return parse_config(data, base=Path("."))


def load(**extra: Any) -> dict[str, Any]:
    return {"load": "m:C", **extra}


def test_fallchain_cycle_is_config_error() -> None:
    providers = {"a": load(fallback=["b"]), "b": load(fallback=["a"])}
    with pytest.raises(ConfigError) as err:
        parse(providers)
    assert str(err.value) == "fallback cycle: a -> b -> a"
    providers = {"a": load(), "b": load(fallback=["a"])}
    rules = [{"provider": "a", "label": "x", "fallback": ["b"]}]
    with pytest.raises(ConfigError) as err:
        parse(providers, rules)
    assert str(err.value) == "fallback cycle: a -> b -> a"


def test_fallchain_unknown_provider() -> None:
    with pytest.raises(ConfigError) as err:
        parse({"a": load(fallback=["x"])})
    assert str(err.value) == "providers.a.fallback names unknown provider 'x'"
    with pytest.raises(ConfigError) as err:
        parse({"a": load(fallback=["a"])})
    assert str(err.value) == "providers.a.fallback lists 'a' itself"
    with pytest.raises(ConfigError) as err:
        parse({"a": load()}, [{"provider": "a", "label": "x", "fallback": ["y"]}])
    assert str(err.value) == "routing.rules[0].fallback names unknown provider 'y'"


def test_fallchain_chain_order() -> None:
    config = parse(
        {
            "a": load(fallback=["b"]),
            "b": load(fallback=["c"]),
            "c": load(),
            "d": {"url": "wss://far.example.com", "agent": "x", "fallback": ["a"]},
        },
        [
            {"provider": "a", "label": "x*", "fallback": ["c", "b"]},
            {"provider": "d", "label": "far*", "fallback": []},
        ],
    )
    assert config.providers["a"].fallback == ("b",)
    assert config.routing.rules[0].fallback == ("c", "b")
    # A rule's list replaces the provider's; duplicates are dropped.
    assert fallback_chain(config, AgentCall(0, "p", {}, None, "x1")) == ("a", "c", "b")
    # No rule matches: the default's own list, expanded depth first.
    assert fallback_chain(config, AgentCall(0, "p")) == ("a", "b", "c")
    # A rule with an empty list falls back to nothing.
    assert fallback_chain(config, AgentCall(0, "p", {}, None, "far1")) == ("d",)
    # A pinned call uses the provider's list.
    pinned = AgentCall(0, "p", {"x": {"provider": "d"}}, None, "x1")
    assert fallback_chain(config, pinned) == ("d", "a", "b", "c")
