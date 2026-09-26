"""The shipped schema gate, with pytest's skip policy applied.

The logic lives in `ahp_protocol.conformance.schemas` so a host and a
client can share it instead of keeping two copies -- which is exactly the drift
the extraction existed to prevent. What stays here is the one thing that is a
TEST decision rather than a library one: a missing `jsonschema` skips the suite
rather than failing the build.
"""

from __future__ import annotations

import pytest

pytest.importorskip("jsonschema")

from ahp_protocol.conformance.schemas import (
    SCHEMA_DIR,
    action_definition_for,
    assert_valid_action,
    assert_valid_result,
    assert_valid_state,
    load,
    validate_against,
)

__all__ = [
    "SCHEMA_DIR",
    "action_definition_for",
    "assert_valid_action",
    "assert_valid_result",
    "assert_valid_state",
    "load",
    "validate_against",
]
