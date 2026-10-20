"""Error kinds: DispatchError and the errorType classification table (M16)."""

from __future__ import annotations

import pytest

from ahp_host_flow.dispatch import FlowAgentError
from ahp_host_flow.engine import DispatchError, classify
from ahp_host_flow.engine.errorkinds import ERROR_TABLE, PROVIDER_LOAD, REMOTE_IDLE, REMOTE_LOST


@pytest.mark.parametrize(
    ("error_type", "resumable", "message", "expected"),
    [
        ("acp.start", False, "", ("unavailable", True)),
        ("acp.agentExited", False, "", ("unavailable", True)),
        ("acp.refusal", False, "", ("refused", False)),
        ("acp.max_tokens", False, "", ("failed", False)),
        ("claude.exited", False, "", ("unavailable", True)),
        ("claude.error_during_execution", True, "boom", ("unavailable", True)),
        (
            "claude.error_during_execution",
            True,
            "API Error: 429 Too Many Requests",
            ("rate_limited", True),
        ),
        ("claude.error_during_execution", True, "overloaded (529)", ("unavailable", True)),
        ("claude.error_during_execution", False, "429", ("failed", False)),
        (PROVIDER_LOAD, False, "", ("unavailable", False)),
        (REMOTE_LOST, False, "", ("unavailable", True)),
        (REMOTE_IDLE, False, "", ("timeout", True)),
        ("agent.turn", False, "", ("failed", False)),
        ("agent.turn", True, "", ("failed", True)),
        ("acp.-32603", False, "", ("failed", False)),
        ("acp.max_turn_requests", False, "", ("failed", False)),
        (
            "acp.-32603",
            False,
            "rate limited: the endpoint answered HTTP 429: Rate limit exceeded",
            ("rate_limited", True),
        ),
        ("acp.-32603", False, "Too Many Requests: rate limit", ("rate_limited", True)),
        ("acp.-32603", False, "HTTP/1.1 429 Too Many Requests", ("rate_limited", True)),
        ("acp.-32603", False, "upstream status code: 429", ("rate_limited", True)),
        ("acp.-32603", False, "SyntaxError at line 429 of build.py", ("failed", False)),
        ("acp.-32603", False, "read 429 files, then failed", ("failed", False)),
        ("acp.-32603", False, "HTTP 4290 tokens", ("failed", False)),
    ],
)
def test_errkind_table_row(
    error_type: str, resumable: bool, message: str, expected: tuple[str, bool]
) -> None:
    assert classify(error_type, resumable=resumable, message=message) == expected


def test_errkind_dispatch_error_str() -> None:
    exc = DispatchError("timeout", True, "x")
    assert str(exc) == "x"
    assert (exc.kind, exc.transient) == ("timeout", True)
    flow = FlowAgentError("m")
    assert (flow.kind, flow.transient, str(flow)) == ("failed", False, "m")


#: One input per `ERROR_TABLE` row (the README's table), by its pattern.
_ROW_SAMPLES: dict[str, tuple[str, bool, str]] = {
    "acp.start": ("acp.start", False, ""),
    "acp.agentExited": ("acp.agentExited", False, ""),
    "acp.refusal": ("acp.refusal", False, ""),
    "acp.max_tokens": ("acp.max_tokens", False, ""),
    "acp.* (rate-limit text)": ("acp.-32603", False, "HTTP 429"),
    "claude.exited": ("claude.exited", False, ""),
    "claude.* (resumable, rate-limit text)": ("claude.x", True, "rate limit reached"),
    "claude.* (resumable)": ("claude.x", True, "overloaded"),
    "claude.*": ("claude.x", False, ""),
    PROVIDER_LOAD: (PROVIDER_LOAD, False, ""),
    REMOTE_LOST: (REMOTE_LOST, False, ""),
    REMOTE_IDLE: (REMOTE_IDLE, False, ""),
    "anything else": ("something.else", False, ""),
}


def test_errkind_table_every_row_classifies_as_listed() -> None:
    # The table is what the README shows; `classify` must agree with each row, and a new row
    # needs a sample here.
    assert [pattern for pattern, _ in ERROR_TABLE] == list(_ROW_SAMPLES)
    for pattern, kind in ERROR_TABLE:
        error_type, resumable, message = _ROW_SAMPLES[pattern]
        assert classify(error_type, resumable=resumable, message=message)[0] == kind, pattern
