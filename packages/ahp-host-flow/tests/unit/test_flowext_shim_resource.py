"""The dual-target shim ships with the package and matches the repository copy."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from ahp_host_flow import shim
from ahp_host_flow.cli import main

REPO_SHIM = Path(__file__).resolve().parents[2] / "shim" / "flow-shim.js"
DUAL = Path(__file__).resolve().parents[2] / "conformance" / "shim" / "dual_target.js"


def test_flowext_shim_resource_installed() -> None:
    resource = resources.files("ahp_host_flow.shim").joinpath(shim.SHIM_NAME)
    assert resource.is_file()
    assert resource.read_bytes() == REPO_SHIM.read_bytes()
    assert shim.source() == REPO_SHIM.read_text(encoding="utf-8")


def test_flowext_shim_resource_embedded_in_dual_target() -> None:
    assert shim.source().strip() in DUAL.read_text(encoding="utf-8")


def test_flowext_shim_cli_prints_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["shim"]) == 0
    out = capsys.readouterr().out
    assert out == shim.source()
    assert "FLOW_SHIM" in out
