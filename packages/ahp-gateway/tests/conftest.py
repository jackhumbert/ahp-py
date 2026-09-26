"""Fixtures. The fleet itself is built in `tests.fleet`."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agent_host_broker.registry import NodeRecord
from tests.fleet import DEV, Fleet, echo_host, everyone_is_a_dev


@pytest.fixture
async def fleet() -> AsyncIterator[Fleet]:
    """Two dev nodes offering different agents, and an ops node no dev can use."""
    hosts = {"node-a": echo_host("alpha"), "node-b": echo_host("beta"), "ops": echo_host("gamma")}
    records = [
        NodeRecord("node-a", "mem://node-a", DEV),
        NodeRecord("node-b", "mem://node-b", DEV),
        NodeRecord("ops", "mem://ops", frozenset({"ops"})),
    ]
    built = Fleet(hosts, records, everyone_is_a_dev)
    yield built
    await built.aclose()
