"""The heartbeat descriptor: its builder and its schema branch."""

from __future__ import annotations

import jsonschema
import pytest

import ahp_protocol.ext.flow as flow
from ahp_protocol.ext.flow import DESCRIPTOR_SCHEMA

VALIDATOR = jsonschema.Draft202012Validator(DESCRIPTOR_SCHEMA)


def test_heartbeat_flow_ext_builder() -> None:
    built = flow.heartbeat(running=2, tick=3)
    assert built == {"heartbeat": {"running": 2, "tick": 3}}
    assert flow.HEARTBEAT_KIND in built
    VALIDATOR.validate(built)
    VALIDATOR.validate(flow.heartbeat(running=0, tick=1))
    with pytest.raises(ValueError, match="running"):
        flow.heartbeat(running=-1, tick=1)
    with pytest.raises(ValueError, match="tick"):
        flow.heartbeat(running=1, tick=0)
    assert not VALIDATOR.is_valid({"heartbeat": {"running": 1, "tick": 0}})
    assert not VALIDATOR.is_valid({"heartbeat": {"running": 1, "tick": 1, "extra": 1}})
    assert not VALIDATOR.is_valid({"heartbeat": {"running": 1}})
