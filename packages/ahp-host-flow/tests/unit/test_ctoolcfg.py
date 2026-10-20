"""`[workflows] client_tools` and the `ClientTools` filter (plan §8 M17 item 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.clienttools import ClientTools
from ahp_host_flow.config import ConfigError, parse_config


def parse(workflows: dict[str, Any], base: Path) -> Any:
    data = {
        "providers": {"p": {"load": "m:C"}},
        "routing": {"default": "p"},
        "workflows": workflows,
    }
    return parse_config(data, base=base).workflows.client_tools


def test_ctoolcfg_default_true(tmp_path: Path) -> None:
    assert parse({}, tmp_path) is True


def test_ctoolcfg_false_and_list(tmp_path: Path) -> None:
    assert parse({"client_tools": False}, tmp_path) is False
    assert parse({"client_tools": ["a", "b"]}, tmp_path) == ("a", "b")
    assert parse({"client_tools": []}, tmp_path) == ()


def test_ctoolcfg_bad_values(tmp_path: Path) -> None:
    msg = "workflows.client_tools must be true, false or an array of tool names"
    for bad in ("all", 3, ["a", 1]):
        with pytest.raises(ConfigError) as exc:
            parse({"client_tools": bad}, tmp_path)
        assert str(exc.value) == msg
    with pytest.raises(ConfigError) as exc:
        parse({"client_tools": ["a", "a"]}, tmp_path)
    assert str(exc.value) == "workflows.client_tools has duplicates"


def tool(name: str) -> dict[str, Any]:
    return {"name": name}


def test_ctoolcfg_filters() -> None:
    fleet = [
        tool(n) for n in ("start_session", "wait_for_sessions", "read_session", "stop_session")
    ]
    lookup, rename = tool("lookup"), tool("rename")
    active: list[dict[str, Any]] = [
        {"clientId": "gw", "tools": fleet},
        {"clientId": "ed", "tools": [lookup, rename]},
        {"clientId": "ed2", "tools": [lookup]},
        {"tools": [tool("orphan")]},
    ]
    allowed = ClientTools(True, active)
    assert [c["clientId"] for c in allowed.clients()] == ["ed", "ed2"]
    assert allowed.owner("lookup") == "ed"
    assert allowed.owner("start_session") is None
    assert [d["name"] for d in allowed.definitions()] == ["lookup", "rename"]
    assert allowed.first() == ("ed", (lookup, rename))

    some = ClientTools(("lookup",), active)
    assert [d["name"] for d in some.definitions()] == ["lookup"]

    off = ClientTools(False, active)
    assert off.clients() == []
    assert off.definitions() == []
    assert off.owner("lookup") is None
    assert off.first() == (None, ())
