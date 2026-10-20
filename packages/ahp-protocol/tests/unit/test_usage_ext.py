"""The usage scope marker (ahp_protocol.ext.usage)."""

from __future__ import annotations

from ahp_protocol.ext.usage import USAGE_META_KEY, is_request_scoped, request_usage_meta


def test_usage_ext_request_scope() -> None:
    meta = request_usage_meta()
    assert meta == {USAGE_META_KEY: {"scope": "request"}}
    assert is_request_scoped(meta)
    assert not is_request_scoped(None)
    assert not is_request_scoped("request")
    assert not is_request_scoped({})
    assert not is_request_scoped({USAGE_META_KEY: "request"})
    assert not is_request_scoped({USAGE_META_KEY: {"scope": "turn"}})
