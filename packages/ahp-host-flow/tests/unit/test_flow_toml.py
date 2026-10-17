"""flow.toml parsing: the full example, path resolution, and every rejection."""

from __future__ import annotations

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
        ({"providers": _providers(), "workflows": {"sources": ["client"]}}, "client"),
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


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no config file"):
        load_config(tmp_path / "nope.toml")


def test_bad_toml(tmp_path: Path) -> None:
    path = tmp_path / "flow.toml"
    path.write_text("providers = [", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"flow.toml"):
        load_config(path)
