"""`canonical_channel` folds URI-equivalent spellings of the singleton channels."""

from __future__ import annotations

import pytest

from ahp_protocol import canonical_channel
from ahp_protocol.channels import AUTOMATIONS_URI, ROOT_URI


@pytest.mark.parametrize("tail", ["", "/", "//", "///"])
def test_empty_authority_spellings_fold_to_the_constant(tail: str) -> None:
    assert canonical_channel(f"ahp-automations:{tail}") == AUTOMATIONS_URI
    assert canonical_channel(f"ahp-root:{tail}") == ROOT_URI


@pytest.mark.parametrize(
    "value",
    [
        "ahp-automations://x",
        "ahp-root:/x",
        "ahp-session:/abc",
        "ahp-automation:/",
        "ahp-automations",
        "",
        "other:",
    ],
)
def test_anything_else_is_unchanged(value: str) -> None:
    assert canonical_channel(value) == value


def test_a_non_string_passes_through() -> None:
    assert canonical_channel(None) is None  # type: ignore[arg-type]
