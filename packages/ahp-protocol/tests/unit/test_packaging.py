"""The wheel must contain the corpus it claims to ship.

This package's whole proposition is that another implementation can run the same
gate we run. The sibling host shipped a `conformance` subpackage whose
`CORPUS_ROOT` walked up to a `vendor/` directory the wheel did not contain --
so `pip install agent-host-server` gave you a fixture loader and no fixtures,
and nobody noticed because every test ran from a source checkout where the
fallback path resolves.

A source checkout cannot catch that. So this test builds a real wheel and looks
inside it.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytest.importorskip("build", reason="the packaging gate needs `build` installed")


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> zipfile.ZipFile:
    out = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(ROOT)],
        check=True,
        capture_output=True,
    )
    built = sorted(out.glob("*.whl"))
    assert len(built) == 1, f"expected exactly one wheel, got {built}"
    return zipfile.ZipFile(built[0])


def test_wheel_ships_the_reducer_corpus(wheel: zipfile.ZipFile) -> None:
    names = wheel.namelist()
    fixtures = [
        n
        for n in names
        if n.startswith("agent_host_protocol/conformance/_upstream/test-cases/reducers/")
        and n.endswith(".json")
    ]
    assert len(fixtures) == 256


def test_wheel_ships_the_round_trip_corpus(wheel: zipfile.ZipFile) -> None:
    names = wheel.namelist()
    fixtures = [
        n
        for n in names
        if n.startswith("agent_host_protocol/conformance/_upstream/test-cases/round-trips/")
        and n.endswith(".json")
    ]
    assert len(fixtures) == 39


def test_wheel_ships_the_pin_and_the_schemas(wheel: zipfile.ZipFile) -> None:
    names = set(wheel.namelist())
    assert "agent_host_protocol/conformance/_upstream/PIN.json" in names
    # The schemas cannot validate AHP traffic (see UPSTREAM.md), but two tests
    # pin *why*, and a downstream running our suite needs them present to do so.
    assert "agent_host_protocol/conformance/_upstream/schema/actions.schema.json" in names


def test_wheel_is_typed(wheel: zipfile.ZipFile) -> None:
    """PEP 561. Without the marker every re-exported type is `Any` downstream,
    which silently defeats the `mypy --strict` story both peers depend on."""
    assert "agent_host_protocol/py.typed" in set(wheel.namelist())


def test_installed_layout_resolves_without_the_source_checkout(wheel: zipfile.ZipFile) -> None:
    """`_corpus_root` prefers the packaged tree; assert the path it looks for.

    A rename on either side of this pair silently reverts to the fallback, which
    works in CI and fails for every installed user -- exactly the failure mode
    this module exists to prevent.
    """
    assert any(n.startswith("agent_host_protocol/conformance/_upstream/") for n in wheel.namelist())
