"""flow.toml `[journal] keep_source` (plan §8 M17 item 4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.config import ConfigError, JournalConfig, parse_config


def _config(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    return {"providers": {"a": {"load": "pkg.mod:Cls"}}, **extra}


def test_journalcfg_default_masks(tmp_path: Path) -> None:
    assert parse_config(_config(tmp_path), base=tmp_path).journal == JournalConfig(False)


def test_journalcfg_keep_source(tmp_path: Path) -> None:
    data = _config(tmp_path, journal={"keep_source": True})
    assert parse_config(data, base=tmp_path).journal.keep_source is True


@pytest.mark.parametrize(
    ("journal", "match"),
    [
        ({"keep_source": "yes"}, "journal.keep_source must be true or false"),
        ({"keep": True}, "unknown key"),
        ("on", "journal must be a table"),
    ],
)
def test_journalcfg_rejected(tmp_path: Path, journal: Any, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_config(_config(tmp_path, journal=journal), base=tmp_path)
