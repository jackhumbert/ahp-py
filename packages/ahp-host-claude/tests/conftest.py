"""Keep every test's chat directory out of the real home directory."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _state_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ahp_host_claude.provider.DEFAULT_STATE", tmp_path / "state")
