"""`flow.toml` [verbs] and lazy verb loading by import path. No Node."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.config import ConfigError, FlowConfig, ProviderConfig, parse_config
from ahp_host_flow.engine import RunOptions, VerbCall, VerbContext, verb_name_error
from ahp_host_flow.loading import lazy_verbs, load_provider

_PROVIDERS = {"p": {"load": "tests.flow_fakes:StrippedEchoProvider"}}


def _config(verbs: Any) -> FlowConfig:
    return parse_config({"providers": _PROVIDERS, "verbs": verbs}, base=Path("."))


def _call(name: str) -> VerbCall:
    return VerbCall(name, ("x",), 1, f"verb-{name}", None, None, VerbContext())


def test_customverb_config_parsed() -> None:
    config = _config(
        {"tool": "tests.hook_fakes:echo_verb", "approve": "tests.hook_fakes:echo_verb"}
    )
    assert list(config.verbs) == ["tool", "approve"]
    assert config.verbs["tool"] == "tests.hook_fakes:echo_verb"
    assert parse_config({"providers": _PROVIDERS}, base=Path(".")).verbs == {}


@pytest.mark.parametrize(
    ("verbs", "match"),
    [
        ({"agent": "m:f"}, "is reserved: Claude Code"),
        ({"meta": "m:f"}, "is reserved: Claude Code"),
        ({"budget": "m:f"}, "is reserved: Claude Code"),
        ({"_x": "m:f"}, "verbs._x"),
        ({"a-b": "m:f"}, "verbs.a-b"),
        ({"class": "m:f"}, "verbs.class"),
        ({"tool": 5}, "must be"),
        ({"tool": "nocolon"}, "must be"),
    ],
)
def test_customverb_config_rejected(verbs: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match) as info:
        _config(verbs)
    if "reserved" in match:
        assert "agent, parallel, pipeline, phase, log, args, budget, workflow, meta" in str(
            info.value
        )


@pytest.mark.parametrize(
    "name",
    [
        # JavaScript standard globals
        "globalThis", "Object", "Array", "Promise", "JSON", "Math", "TypeError", "Map",
        "Uint8Array", "eval", "parseInt", "encodeURIComponent", "undefined", "console",
        # names the sandbox keeps absent
        "require", "process", "module", "exports", "Buffer", "setTimeout", "queueMicrotask",
        "structuredClone", "fetch",
        # Python builtins
        "print", "open", "len", "type", "dict", "exec", "getattr", "isinstance", "id",
    ],
)  # fmt: skip
def test_customverb_config_shadowing_refused(name: str) -> None:
    reason = verb_name_error(name)
    assert reason is not None
    assert repr(name) in reason
    assert "shadow" in reason or "reserved word" in reason
    with pytest.raises(ConfigError, match=r"shadow|reserved word"):
        _config({name: "m:f"})
    with pytest.raises(ValueError, match=r"shadow|reserved word"):
        RunOptions(verbs={name: _noop})


@pytest.mark.parametrize("name", ["ask", "approve", "canvas", "tool", "render", "search"])
def test_customverb_config_shadowing_allows(name: str) -> None:
    assert verb_name_error(name) is None
    assert name in _config({name: "m:f"}).verbs


async def _noop(call: VerbCall) -> Any:
    return None


async def test_customverb_config_lazy_loading() -> None:
    config = _config(
        {
            "gone": "tests.no_such_module:fn",
            "good": "tests.hook_fakes:echo_verb",
            "notcall": "tests.hook_fakes:ECHO_CALLS",
        }
    )
    handlers = lazy_verbs(config)  # building them imports nothing
    assert list(handlers) == ["gone", "good", "notcall"]
    errors: list[BaseException] = []
    for _ in range(2):
        with pytest.raises(ConfigError, match="cannot import") as info:
            await handlers["gone"](_call("gone"))
        errors.append(info.value)
    assert errors[0] is errors[1]
    assert await handlers["good"](_call("good")) == {"name": "good", "args": ["x"]}
    with pytest.raises(ConfigError, match="is not callable"):
        await handlers["notcall"](_call("notcall"))


def test_customverb_config_provider_messages_unchanged() -> None:
    spec = ProviderConfig("x", "tests.no_such_module:Provider")
    with pytest.raises(ConfigError, match=r"providers\.x\.load: cannot import"):
        load_provider(spec)
