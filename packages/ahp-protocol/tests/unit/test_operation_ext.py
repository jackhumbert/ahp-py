"""The changeset operation ``_meta`` extension, exactly."""

from __future__ import annotations

from ahp_protocol.ext.operation import OPERATION_META_KEY, operation_meta, operation_params


def test_opmeta_round_trip() -> None:
    assert OPERATION_META_KEY == "ahp-py/operation"
    meta = operation_meta({"path": "a.txt", "n": 2})
    assert meta == {"ahp-py/operation": {"path": "a.txt", "n": 2}}
    assert operation_params(meta) == {"path": "a.txt", "n": 2}
    assert operation_params({**meta, "other": 1}) == {"path": "a.txt", "n": 2}


def test_opmeta_non_mapping_is_empty() -> None:
    assert operation_params(None) == {}
    assert operation_params("x") == {}
    assert operation_params({OPERATION_META_KEY: 3}) == {}
    assert operation_params({}) == {}
