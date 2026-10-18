"""The delegation session origin: builder and reader."""

from __future__ import annotations

import pytest

from ahp_protocol.ext.delegation import (
    DELEGATION_KIND,
    ORIGIN_META_KEY,
    delegation_origin,
    parse_delegation,
)

ORIGIN = {
    "kind": "ahp-py/delegation",
    "session": "flow:/run-1",
    "chat": "ahp-chat:/w",
    "toolCallId": "agent-0",
}


def test_delegation_constants() -> None:
    assert ORIGIN_META_KEY == "ahp-py/origin"
    assert DELEGATION_KIND == "ahp-py/delegation"


def test_delegation_origin_builds_the_four_keys() -> None:
    assert delegation_origin("flow:/run-1", "ahp-chat:/w", "agent-0") == ORIGIN


@pytest.mark.parametrize("bad", [("", "c", "t"), ("s", "", "t"), ("s", "c", ""), ("s", "c", 1)])
def test_delegation_origin_rejects_empty_or_non_strings(bad: tuple[object, ...]) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        delegation_origin(*bad)  # type: ignore[arg-type]


def test_delegation_parse_returns_a_fresh_dict_and_drops_extra_keys() -> None:
    value = {**ORIGIN, "extra": 1}
    parsed = parse_delegation(value)
    assert parsed == ORIGIN
    assert parsed is not value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "x",
        [],
        {"kind": "automation", "session": "s", "chat": "c", "toolCallId": "t"},
        {"kind": "x"},
        {**ORIGIN, "session": ""},
        {**ORIGIN, "chat": 3},
        {k: v for k, v in ORIGIN.items() if k != "toolCallId"},
    ],
)
def test_delegation_parse_ignores_anything_else(value: object) -> None:
    assert parse_delegation(value) is None
