"""flow.toml `[limits]` and `call_timeout` (plan §8 M16)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.config import ConfigError, FlowConfig, LimitsConfig, parse_config

PROVIDER: dict[str, Any] = {"claude": {"load": "m:C"}}


def parse(
    limits: dict[str, Any] | None = None, providers: dict[str, Any] | None = None
) -> FlowConfig:
    data: dict[str, Any] = {"providers": providers if providers is not None else PROVIDER}
    if limits is not None:
        data["limits"] = limits
    if len(data["providers"]) > 1:
        # routing.default is mandatory with more than one provider (config._parse_routing).
        data["routing"] = {"default": next(iter(data["providers"]))}
    return parse_config(data, base=Path("."))


def test_flowlimits_config_parses() -> None:
    assert parse().limits == LimitsConfig()
    config = parse(
        {
            "budget": 100,
            "input_budget": 0,
            "cost_budget": 2,
            "wall_budget": 30,
            "timeout": 60.5,
            "concurrency": 3,
            "reserve": "mean",
            "count_cache": False,
        }
    )
    assert config.limits == LimitsConfig(
        budget=100,
        input_budget=0,
        cost_budget=2.0,
        wall_budget=30.0,
        timeout=60.5,
        concurrency=3,
        reserve="mean",
        count_cache=False,
    )
    assert parse({"reserve": 0}).limits.reserve == 0
    assert parse({"reserve": 50}).limits.reserve == 50


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"reserve": "avg"}, "limits.reserve"),
        ({"reserve": -1}, "limits.reserve"),
        ({"budget": -1}, "limits.budget"),
        ({"budget": 1.5}, "limits.budget"),
        ({"budget": True}, "limits.budget"),
        ({"input_budget": -1}, "limits.input_budget"),
        ({"cost_budget": -0.5}, "limits.cost_budget"),
        ({"cost_budget": "free"}, "limits.cost_budget"),
        ({"wall_budget": 0}, "limits.wall_budget"),
        ({"timeout": 0}, "limits.timeout"),
        ({"timeout": "soon"}, "limits.timeout"),
        ({"concurrency": 0}, "limits.concurrency"),
        ({"count_cache": "yes"}, "limits.count_cache"),
        ({"budgets": 1}, "unknown key"),
    ],
)
def test_flowlimits_config_rejects(limits: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(limits)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
@pytest.mark.parametrize("key", ["wall_budget", "timeout", "heartbeat"])
def test_flowlimits_seconds_must_be_finite(key: str, value: float) -> None:
    with pytest.raises(ConfigError, match=f"limits.{key} must be a positive number of seconds"):
        parse({key: value})


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
@pytest.mark.parametrize("key", ["call_timeout", "idle_timeout"])
def test_flowlimits_provider_seconds_must_be_finite(key: str, value: float) -> None:
    with pytest.raises(ConfigError, match=f"providers.claude.{key} must be a positive number"):
        parse(providers={"claude": {"load": "m:C", key: value}})


@pytest.mark.parametrize(
    ("quotas", "message"),
    [
        ({"tokens_per_day": 0}, r"^quotas\.tokens_per_day must be an integer >= 1$"),
        ({"tokens_per_run": 1.5}, r"^quotas\.tokens_per_run must be an integer >= 1$"),
        ({"tokens_per_run": True}, r"^quotas\.tokens_per_run must be an integer >= 1$"),
        ({"cost_per_day": float("nan")}, r"^quotas\.cost_per_day must be a number > 0$"),
        ({"cost_per_run": float("inf")}, r"^quotas\.cost_per_run must be a number > 0$"),
    ],
)
def test_flowlimits_quotas_rejects(quotas: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config({"providers": PROVIDER, "quotas": quotas}, base=Path("."))


def test_flowlimits_quotas_parse() -> None:
    quotas = {"tokens_per_day": 10, "tokens_per_run": 5, "cost_per_day": 2, "cost_per_run": 0.5}
    config = parse_config({"providers": PROVIDER, "quotas": quotas}, base=Path("."))
    assert config.quotas.tokens_per_day == 10
    assert config.quotas.cost_per_day == 2.0


def test_flowlimits_call_timeout_all_kinds() -> None:
    config = parse(
        providers={
            "local": {"load": "m:C", "call_timeout": 5},
            "fleet": {"via": "fleet", "node": "n", "agent": "a", "call_timeout": 6.5},
            "remote": {"url": "wss://x.example.com", "agent": "a", "call_timeout": 7},
            "none": {"load": "m:C"},
        }
    )
    got = {name: spec.call_timeout for name, spec in config.providers.items()}
    assert got == {"local": 5.0, "fleet": 6.5, "remote": 7.0, "none": None}
    for bad in (0, -1, "soon", True):
        with pytest.raises(ConfigError, match="call_timeout"):
            parse(providers={"claude": {"load": "m:C", "call_timeout": bad}})
