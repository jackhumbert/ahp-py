"""The routed provider and model in the resume key (`[resume] key`). No Node."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.config import ConfigError, FlowConfig, parse_config
from ahp_host_flow.engine import AgentCall, RunOptions, call_key, load_journal
from ahp_host_flow.loading import resume_key_hook
from ahp_host_flow.routing import resolved_route
from ahp_host_flow.sandbox import run_script

from ..fakes import FakeDispatcher  # noqa: TID252

LOAD = "tests.flow_fakes:RecordingProvider"


def _config(extra: dict[str, Any] | None = None, **providers: Any) -> FlowConfig:
    table = providers or {"a": {"load": LOAD, "models": {"fast": "m-1"}}, "b": {"load": LOAD}}
    routing = {} if providers else {"routing": {"default": "a"}}
    return parse_config({"providers": table, **routing, **(extra or {})}, base=Path("."))


def _phase_config(provider: str) -> FlowConfig:
    return _config(
        {
            "resume": {"key": "model"},
            "routing": {
                "default": "p1",
                "rules": [{"phase": "Review", "provider": provider}],
            },
        },
        p1={"load": LOAD},
        p2={"load": LOAD},
    )


def test_keyhook_route_resolved() -> None:
    config = _config({"routing": {"default": "a"}})
    pinned = AgentCall(0, "x", {"x": {"provider": "b"}, "model": "fast"})
    assert resolved_route(config, pinned, None) == {"provider": "b", "model": "fast"}
    alias = AgentCall(0, "x", {"model": "fast"})
    assert resolved_route(config, alias, None) == {"provider": "a", "model": "m-1"}
    unknown = AgentCall(0, "x", {"model": "other"})
    assert resolved_route(config, unknown, None) == {"provider": "a", "model": "other"}
    plain = AgentCall(0, "x")
    assert resolved_route(config, plain, "m-1") == {"provider": "a", "model": "m-1"}
    assert resolved_route(config, plain, "m-9") == {"provider": "a", "model": None}
    assert resolved_route(config, plain, None) == {"provider": "a", "model": None}


def test_keyhook_route_config() -> None:
    assert _config().resume.key == "default"
    for key in ("default", "model", "tests.hook_fakes:route_key"):
        assert _config({"resume": {"key": key}}).resume.key == key
    assert _config({"resume": {}}).resume.key == "default"
    with pytest.raises(ConfigError, match=r'resume\.key must be "default", "model" or'):
        _config({"resume": {"key": "bogus"}})
    with pytest.raises(ConfigError, match="got 5"):
        _config({"resume": {"key": 5}})
    with pytest.raises(ConfigError, match="resume"):
        _config({"resume": {"key": "model", "other": 1}})


async def _run(source: str | None, dispatcher: FakeDispatcher, options: RunOptions) -> Any:
    return await asyncio.wait_for(
        run_script(source, node=None, dispatcher=dispatcher, options=options, filename="w.py"), 30
    )


def _options(tmp_path: Path, config: FlowConfig | None, **kwargs: Any) -> RunOptions:
    if config is not None:
        key_extra, key_mode = resume_key_hook(config, None)
        kwargs.update(key_extra=key_extra, key_mode=key_mode)
    return RunOptions(timeout=60, concurrency=4, state_dir=tmp_path / "state", **kwargs)


async def _live(
    tmp_path: Path, script: str, first: FlowConfig | None, second: FlowConfig | None
) -> list[str]:
    """Prompts dispatched live when resuming as the provider does (`continue_run`)."""
    await _run(script, FakeDispatcher(), _options(tmp_path, first, run_id="r1"))
    dispatcher = FakeDispatcher()
    resumed = _options(tmp_path, second, run_id="r2", resume_from="r1", continue_run=True)
    await _run(None, dispatcher, resumed)
    return [c.prompt for c in dispatcher.calls]


async def test_keyhook_route_phase_routing_change(tmp_path: Path) -> None:
    # The changed call comes first: the unchanged calls after it replay from the cache.
    script = (
        "async def main(args):\n"
        "    phase('Review')\n"
        "    await agent('c')\n"
        "    phase('Build')\n"
        "    await agent('a')\n"
        "    await agent('d')\n"
    )
    cfg1 = _phase_config("p1")
    cfg2 = _phase_config("p2")
    assert await _live(tmp_path / "x1", script, cfg1, cfg2) == ["c"]
    assert await _live(tmp_path / "x2", script, cfg1, cfg1) == []
    assert await _live(tmp_path / "x3", script, None, None) == []


async def test_keyhook_route_alias_change(tmp_path: Path) -> None:
    script = (
        "async def main(args):\n"
        "    await agent('b', model='fast')\n"
        "    await agent('a')\n"
        "    await agent('d')\n"
    )

    def config(model: str) -> FlowConfig:
        return _config({"resume": {"key": "model"}}, a={"load": LOAD, "models": {"fast": model}})

    assert await _live(tmp_path / "x1", script, config("m-1"), config("m-2")) == ["b"]
    assert await _live(tmp_path / "x2", script, config("m-1"), config("m-1")) == []
    assert await _live(tmp_path / "x3", script, None, None) == []


async def test_keyhook_route_custom_callable(tmp_path: Path) -> None:
    config = _config({"resume": {"key": "tests.hook_fakes:route_key"}})
    key_extra, key_mode = resume_key_hook(config, None)
    assert key_mode == "tests.hook_fakes:route_key"
    script = "async def main(args):\n    await agent('a')\n    await agent('b')\n"
    dispatcher = FakeDispatcher()
    options = RunOptions(
        timeout=60,
        state_dir=tmp_path / "state",
        run_id="r1",
        key_extra=key_extra,
        key_mode=key_mode,
    )
    await _run(script, dispatcher, options)
    keys = [r.key for r in load_journal(tmp_path / "state", "r1").agents]
    assert keys == [call_key(c, extra={"p": "a", "tag": "v1"}) for c in dispatcher.calls]
    assert keys != [call_key(c) for c in dispatcher.calls]
