"""Node discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host_flow.sandbox import NODE_ENV, find_node, node_version, parse_node_version


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v20.11.1\n", (20, 11, 1)),
        ("22.3.0", (22, 3, 0)),
        ("garbage", None),
        ("", None),
    ],
)
def test_node_discovery_parses_versions(text: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_node_version(text) == expected


def test_node_discovery_env_value_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(NODE_ENV, str(tmp_path / "missing-node"))
    assert find_node() is None


def test_node_discovery_explicit_missing(tmp_path: Path) -> None:
    assert find_node(str(tmp_path / "nope")) is None


@pytest.mark.node
def test_node_discovery_finds_real_node(node_path: str) -> None:
    version = node_version(node_path)
    assert version is not None
    assert version[0] >= 20
