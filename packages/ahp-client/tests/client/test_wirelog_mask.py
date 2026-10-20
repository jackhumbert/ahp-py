"""`WireLog(mask=)`: an opt-in content masker beside the credential redaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahp_client.wirelog import WireLog, read_jsonl

SECRET = "sk-" + "live" * 6


def _mask(text: str) -> str:
    return text.replace(SECRET, "[masked]")


def _frame() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "dispatchAction",
        "params": {
            "action": {"type": "chat/turnStarted", "message": {"text": f"use {SECRET}"}},
            "items": ["plain", SECRET],
            "token": "abc",
            SECRET: 1,
        },
    }


def test_wirelog_mask_a_callable_masks_every_string_value(tmp_path: Path) -> None:
    log = WireLog(tmp_path / "ahp-mask.jsonl", mask=_mask)
    frame = _frame()
    log.record("c2s", frame)
    # The frame that goes on the wire is untouched.
    assert frame == _frame()
    entry = next(iter(read_jsonl(log.path)))
    params = entry["params"]
    assert params["action"]["message"]["text"] == "use [masked]"
    assert params["items"] == ["plain", "[masked]"]
    # Credential keys are still redacted, and keys themselves are left alone.
    assert params["token"] == "<redacted>"
    assert SECRET in params
    assert entry["_ahpLog"]["dir"] == "c2s"


def test_wirelog_mask_an_object_with_a_mask_method(tmp_path: Path) -> None:
    class Masker:
        def mask(self, text: str) -> str:
            return _mask(text)

    log = WireLog(tmp_path / "ahp-mask.jsonl", mask=Masker())
    log.record("s2c", _frame())
    assert SECRET not in json.dumps(next(iter(read_jsonl(log.path)))["params"]["action"])


def test_wirelog_mask_without_one_text_is_kept(tmp_path: Path) -> None:
    log = WireLog(tmp_path / "ahp-mask.jsonl")
    log.record("s2c", _frame())
    assert SECRET in json.dumps(next(iter(read_jsonl(log.path)))["params"]["action"])


def test_wirelog_mask_not_a_masker_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="mask must be"):
        WireLog(tmp_path / "ahp-mask.jsonl", mask=42)  # type: ignore[arg-type]
