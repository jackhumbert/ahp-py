"""flow.toml parsing: the full example, path resolution, and every rejection."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.config import (
    DEFAULT_STATE_DIR,
    ConfigError,
    FlowConfig,
    ProviderConfig,
    RedactConfig,
    Routing,
    RoutingRule,
    WorkflowsConfig,
    load_config,
    make_redactor,
    parse_config,
)


def _providers(**extra: Any) -> dict[str, Any]:
    return {"a": {"load": "pkg.mod:Cls", **extra}}


def test_full_example(tmp_path: Path) -> None:
    state = (tmp_path / "state").as_posix()
    cfg_file = (tmp_path / "b.toml").as_posix()
    wf = (tmp_path / "wf").as_posix()
    toml = f"""
state_dir = "{state}"
node = "/usr/bin/node"

[providers.claude]
load = "pkg.claude:ClaudeProvider"
approval_mode = "acceptEdits"
effort = "high"
config = {{ cwd = "/work", flag = true }}

[providers.claude.models]
fast = "model-fast"
deep = "model-deep"

[providers.other-1]
load = "pkg.other:OtherProvider"
config = "{cfg_file}"

[routing]
default = "claude"

[[routing.rules]]
provider = "other-1"
model = "deep"

[[routing.rules]]
provider = "other-1"
phase = "Review"
label = "rev*"

[[routing.rules]]
provider = "claude"
label = "x?"

[workflows]
dirs = ["{wf}"]
sources = ["workspace", "host"]
"""
    path = tmp_path / "flow.toml"
    path.write_text(toml, encoding="utf-8")
    cfg = load_config(path)
    assert cfg == FlowConfig(
        providers={
            "claude": ProviderConfig(
                "claude",
                "pkg.claude:ClaudeProvider",
                config={"cwd": "/work", "flag": True},
                models={"fast": "model-fast", "deep": "model-deep"},
                approval_mode="acceptEdits",
                effort="high",
            ),
            "other-1": ProviderConfig("other-1", "pkg.other:OtherProvider", config=Path(cfg_file)),
        },
        routing=Routing(
            "claude",
            (
                RoutingRule("other-1", model="deep"),
                RoutingRule("other-1", phase="Review", label="rev*"),
                RoutingRule("claude", label="x?"),
            ),
        ),
        state_dir=Path(state),
        node="/usr/bin/node",
        workflows=WorkflowsConfig((Path(wf),), ("workspace", "host")),
    )
    assert list(cfg.providers) == ["claude", "other-1"]


def test_relative_paths_resolve_against_file(tmp_path: Path) -> None:
    cfg = parse_config(
        {
            "state_dir": "st",
            "providers": _providers(config="p.toml"),
            "workflows": {"dirs": ["wf"]},
        },
        base=tmp_path,
    )
    assert cfg.state_dir == tmp_path / "st"
    assert cfg.providers["a"].config == tmp_path / "p.toml"
    assert cfg.workflows.dirs == (tmp_path / "wf",)


def test_tilde_expands(tmp_path: Path) -> None:
    cfg = parse_config({"state_dir": "~/x", "providers": _providers()}, base=tmp_path)
    assert cfg.state_dir == Path("~/x").expanduser()
    assert "~" not in str(cfg.state_dir)


def test_defaults(tmp_path: Path) -> None:
    cfg = parse_config({"providers": _providers()}, base=tmp_path)
    assert cfg.state_dir == DEFAULT_STATE_DIR.expanduser()
    assert cfg.node is None
    assert cfg.workflows == WorkflowsConfig()
    assert cfg.routing == Routing("a")
    assert cfg.redact == RedactConfig()


def test_masking_config_parsed(tmp_path: Path) -> None:
    cfg = parse_config(
        {
            "providers": _providers(),
            "redact": {"patterns": ["sekrit-[0-9]+"], "builtin": False},
        },
        base=tmp_path,
    )
    assert cfg.redact == RedactConfig(("sekrit-[0-9]+",), False)
    redactor = make_redactor(cfg.redact)
    assert redactor.mask("id sekrit-42 and " + "AKIA" + "TESTONLY" * 2) == (
        "id [redacted] and " + "AKIA" + "TESTONLY" * 2
    )


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"providers": _providers(), "redact": {"bogus": 1}}, "redact"),
        ({"providers": _providers(), "redact": {"patterns": ["("]}}, r"redact.patterns\[0\]"),
        ({"providers": _providers(), "redact": {"patterns": ["a*"]}}, "empty string"),
        ({"providers": _providers(), "redact": {"builtin": "yes"}}, "redact.builtin"),
        ({"providers": _providers(via="x")}, "providers.a.*via"),
        ({"providers": {"a": {}}}, "load"),
        ({"providers": {"a": {"load": "nocolon"}}}, "load"),
        ({"providers": {"bad name": {"load": "m:C"}}}, "bad name"),
        ({"providers": _providers(effort="huge")}, "effort"),
        (
            {"providers": _providers(), "workflows": {"sources": ["x"]}},
            "'x' is not one of client, workspace, user, host",
        ),
        (
            {"providers": _providers(), "workflows": {"sources": ["user", "user"]}},
            "duplicate",
        ),
        (
            {"providers": _providers(), "routing": {"rules": [{"provider": "zz", "label": "x"}]}},
            "zz",
        ),
        (
            {"providers": _providers(), "routing": {"rules": [{"provider": "a"}]}},
            r"rules\[0\]",
        ),
        (
            {"providers": {"a": {"load": "m:C"}, "b": {"load": "m:D"}}},
            "routing.default",
        ),
        ({"providers": {}}, "providers"),
        ({"providers": _providers(models={"x": 1})}, "models.x"),
    ],
)
def test_rejected(data: dict[str, Any], match: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_config(data, base=tmp_path)


def test_python_sources_default(tmp_path: Path) -> None:
    cfg = parse_config({"providers": _providers()}, base=tmp_path)
    assert cfg.workflows.python_sources == ("user", "host")


def test_python_sources_parsed(tmp_path: Path) -> None:
    data = {"providers": _providers(), "workflows": {"python_sources": ["workspace", "host"]}}
    cfg = parse_config(data, base=tmp_path)
    assert cfg.workflows.python_sources == ("workspace", "host")


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (["x"], "is not one of client, workspace, user, host"),
        (["user", "user"], "has duplicates"),
        ("user", "must be an array of strings"),
    ],
)
def test_python_sources_rejected(value: Any, match: str, tmp_path: Path) -> None:
    data = {"providers": _providers(), "workflows": {"python_sources": value}}
    with pytest.raises(ConfigError, match=match):
        parse_config(data, base=tmp_path)


def test_watch_interval_default(tmp_path: Path) -> None:
    cfg = parse_config({"providers": _providers()}, base=tmp_path)
    assert cfg.workflows.watch_interval == 2.0


@pytest.mark.parametrize("value", [0, 0.5])
def test_watch_interval_accepted(value: float, tmp_path: Path) -> None:
    data = {"providers": _providers(), "workflows": {"watch_interval": value}}
    cfg = parse_config(data, base=tmp_path)
    assert cfg.workflows.watch_interval == float(value)
    assert isinstance(cfg.workflows.watch_interval, float)


@pytest.mark.parametrize("value", [-1, True, "2", float("inf")])
def test_watch_interval_refused(value: Any, tmp_path: Path) -> None:
    data = {"providers": _providers(), "workflows": {"watch_interval": value}}
    with pytest.raises(ConfigError, match=r"workflows\.watch_interval must be a number"):
        parse_config(data, base=tmp_path)


def test_workflows_sources_default_excludes_client(tmp_path: Path) -> None:
    assert WorkflowsConfig().sources == ("workspace", "user", "host")
    cfg = parse_config({"providers": _providers()}, base=tmp_path)
    assert cfg.workflows.sources == ("workspace", "user", "host")


def test_workflows_sources_client_opt_in(tmp_path: Path) -> None:
    data = {"providers": _providers(), "workflows": {"sources": ["client", "workspace"]}}
    cfg = parse_config(data, base=tmp_path)
    assert cfg.workflows.sources == ("client", "workspace")


def test_python_sources_rejects_client(tmp_path: Path) -> None:
    data = {"providers": _providers(), "workflows": {"python_sources": ["user", "client"]}}
    with pytest.raises(ConfigError) as info:
        parse_config(data, base=tmp_path)
    assert str(info.value) == (
        "workflows.python_sources: 'client' is never allowed: a client cannot supply a .py workflow"
    )


def test_provider_interactive(tmp_path: Path) -> None:
    on = parse_config({"providers": _providers(interactive=True)}, base=tmp_path)
    assert on.providers["a"].interactive is True
    off = parse_config({"providers": _providers()}, base=tmp_path)
    assert off.providers["a"].interactive is False
    with pytest.raises(ConfigError, match=r"providers\.a\.interactive must be true or false"):
        parse_config({"providers": _providers(interactive="yes")}, base=tmp_path)
    fleet = {"f": {**_FLEET, "interactive": True}}
    with pytest.raises(ConfigError, match=r"unknown key.*interactive"):
        parse_config({"providers": fleet}, base=tmp_path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no config file"):
        load_config(tmp_path / "nope.toml")


def test_bad_toml(tmp_path: Path) -> None:
    path = tmp_path / "flow.toml"
    path.write_text("providers = [", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"flow.toml"):
        load_config(path)


_FLEET = {"via": "fleet", "node": "build-box", "agent": "claude"}
_REMOTE = {"url": "wss://my-mac-mini.example.com", "token_file": "tok", "agent": "claude"}


def test_preamble_config_values(tmp_path: Path) -> None:
    for base in ({"load": "m:C"}, _FLEET, _REMOTE):
        for value in (True, False, "Be terse."):
            cfg = parse_config({"providers": {"a": {**base, "preamble": value}}}, base=tmp_path)
            assert cfg.providers["a"].preamble == value
        for bad in ("", "  ", 3):
            with pytest.raises(ConfigError, match=r"providers\.a\.preamble must be true, false"):
                parse_config({"providers": {"a": {**base, "preamble": bad}}}, base=tmp_path)


def test_preamble_default_is_on(tmp_path: Path) -> None:
    for base in ({"load": "m:C"}, _FLEET, _REMOTE):
        cfg = parse_config({"providers": {"a": dict(base)}}, base=tmp_path)
        assert cfg.providers["a"].preamble is True


def test_provider_kinds_parse(tmp_path: Path) -> None:
    cfg = parse_config(
        {
            "providers": {"a": {"load": "m:C"}, "f": _FLEET, "r": _REMOTE},
            "routing": {"default": "a"},
        },
        base=tmp_path,
    )
    a, f, r = cfg.providers["a"], cfg.providers["f"], cfg.providers["r"]
    assert (a.kind, f.kind, r.kind) == ("inprocess", "fleet", "remote")
    assert (f.via, f.node, f.agent, f.load) == ("fleet", "build-box", "claude", None)
    assert (r.url, r.agent, r.load) == ("wss://my-mac-mini.example.com", "claude", None)
    assert r.token_file == tmp_path / "tok"


@pytest.mark.parametrize(
    ("provider", "match"),
    [
        ({"via": "fleet", "agent": "claude"}, r"providers.a.node is required"),
        ({**_FLEET, "via": "gateway"}, r"providers.a.via must be 'fleet', got 'gateway'"),
        ({**_FLEET, "config": {}}, r"unknown key\(s\) in providers.a: config"),
        ({**_REMOTE, "url": "http://x"}, r"providers.a.url must be a ws:// or wss:// URL"),
        ({"agent": "claude"}, r"providers.a needs one of load, via, url"),
        ({**_FLEET, "load": "m:C"}, r"providers.a: load, via and url are exclusive"),
        ({**_FLEET, "reply_timeout": 0}, r"providers.a.reply_timeout must be a positive"),
        ({**_FLEET, "idle_timeout": 5}, r"unknown key\(s\) in providers.a: idle_timeout"),
        ({**_REMOTE, "idle_timeout": "1"}, r"providers.a.idle_timeout must be a positive"),
        ({**_REMOTE, "reply_timeout": 5}, r"unknown key\(s\) in providers.a: reply_timeout"),
    ],
)
def test_provider_kinds_rejected(provider: dict[str, Any], match: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_config({"providers": {"a": provider}}, base=tmp_path)


def test_provider_kinds_timeouts(tmp_path: Path) -> None:
    cfg = parse_config(
        {
            "providers": {
                "f": {**_FLEET, "reply_timeout": 30},
                "r": {**_REMOTE, "idle_timeout": 1.5},
                "plain": _FLEET,
            },
            "routing": {"default": "f"},
        },
        base=tmp_path,
    )
    assert cfg.providers["f"].reply_timeout == 30.0
    assert cfg.providers["r"].idle_timeout == 1.5
    assert (cfg.providers["plain"].reply_timeout, cfg.providers["plain"].idle_timeout) == (
        None,
        None,
    )


def test_provider_kinds_lazy_providers_list_in_process_only(tmp_path: Path) -> None:
    from ahp_host_flow.loading import LazyProviders

    cfg = parse_config(
        {
            "providers": {"inproc": {"load": "m:C"}, "fleetname": _FLEET},
            "routing": {"default": "inproc"},
        },
        base=tmp_path,
    )
    providers = LazyProviders(cfg)
    assert list(providers) == ["inproc"]
    assert len(providers) == 1
    assert providers.get("fleetname") is None


def test_readme_live_sample_parses(tmp_path: Path) -> None:
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text("utf-8")
    after = readme.split("## Tests against real models", 1)[1]
    block = after.split("```toml\n", 1)[1].split("```", 1)[0]
    cfg = parse_config(tomllib.loads(block), base=tmp_path)
    assert cfg.providers["claude"].load == "ahp_host_claude:from_config"
    assert cfg.providers["glm"].load == "ahp_host_acp:from_config"
    glm_config = cfg.providers["glm"].config
    assert isinstance(glm_config, Path)
    assert glm_config.name == "opencode.toml"
    assert RoutingRule("glm", phase="Review") in cfg.routing.rules
