"""Opt-in live smoke test: a two-call script through the providers a flow.toml configures.

Skipped unless `AHP_FLOW_LIVE_CONFIG` names a flow.toml; it makes real model calls.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from pathlib import Path

import pytest

from ahp_host_flow import load_config
from ahp_host_flow.inprocess import run_in_process

LIVE = os.environ.get("AHP_FLOW_LIVE_CONFIG")

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(
        not LIVE, reason="set AHP_FLOW_LIVE_CONFIG to a flow.toml to run the live smoke test"
    ),
]

SCRIPT = """\
const a = await agent('Reply with exactly one word: alpha', { label: 'first' })
const b = await agent('Reply with exactly one word: beta', { label: 'second' })
return { a, b }
"""


async def test_flow_live_two_calls(node_path: str, tmp_path: Path) -> None:
    assert LIVE
    config = dataclasses.replace(load_config(Path(LIVE)), node=node_path)
    # Two real model turns.
    value = await asyncio.wait_for(run_in_process(config, SCRIPT, root=tmp_path), 600)
    # Wording varies with the model, so only the requested word is checked.
    assert isinstance(value["a"], str)
    assert "alpha" in value["a"].lower()
    assert isinstance(value["b"], str)
    assert "beta" in value["b"].lower()
