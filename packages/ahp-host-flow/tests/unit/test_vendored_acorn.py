"""The vendored acorn parser."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

import ahp_host_flow.sandbox

VENDOR = Path(ahp_host_flow.sandbox.__file__).with_name("vendor")


def _readme_version() -> str:
    match = re.search(
        r"^Version: (\d+\.\d+\.\d+)$", (VENDOR / "README.md").read_text("utf-8"), re.MULTILINE
    )
    assert match is not None
    return match.group(1)


def test_vendored_acorn_license_is_mit() -> None:
    assert "MIT License" in (VENDOR / "LICENSE").read_text("utf-8")


def test_vendored_acorn_readme_version() -> None:
    assert re.fullmatch(r"8\.\d+\.\d+", _readme_version())


def test_vendored_acorn_is_an_es_module() -> None:
    text = (VENDOR / "acorn.mjs").read_text("utf-8")
    assert "export {" in text
    assert "parse" in text


@pytest.mark.node
def test_vendored_acorn_version_matches_readme(node_path: str) -> None:
    url = json.dumps((VENDOR / "acorn.mjs").as_uri())
    script = f"import {{ version }} from {url}; console.log(version)"
    result = subprocess.run(
        [node_path, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == _readme_version()
