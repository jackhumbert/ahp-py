"""Shared fixtures."""

from __future__ import annotations

import pytest

from ahp_host_flow.sandbox import MIN_NODE_MAJOR, NODE_ENV, find_node


@pytest.fixture(scope="session")
def node_path() -> str:
    """A Node >= 20 binary; skips the test when none is found."""
    path = find_node()
    if path is None:
        pytest.skip(f"no Node >= {MIN_NODE_MAJOR} (set {NODE_ENV} or put node on PATH)")
    return path
