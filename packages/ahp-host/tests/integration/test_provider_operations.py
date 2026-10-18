"""Changeset operations a provider declares (`ProvidesOperations`).

The host registers them when it is built, so a provider served by any host has
its operations without the embedder calling `register_operation` for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.changesets import Changeset, ChangesetOperation, FileChange
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import AgentSessionContext, ProvidesOperations
from ahp_host.provider.echo import EchoSession
from tests.unit.hosting import connect, open_session, shut

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_EDIT = FileChange(
    uri="file:///work/main.py",
    before=b"print('one')\n",
    after=b"print('two')\n",
)


class OperatingProvider(EchoProvider):
    """An echo provider that declares `test.op` and keeps its session contexts."""

    def __init__(self, provider_id: str = "echo") -> None:
        super().__init__(provider_id=provider_id)
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []
        self.contexts: list[AgentSessionContext] = []

    async def _handler(self, uri: str, operation: str, target: Mapping[str, Any] | None) -> None:
        self.calls.append((uri, operation, target))

    def changeset_operations(self) -> Mapping[str, Any]:
        return {"test.op": self._handler}

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        self.contexts.append(context)
        return await super().create_session(context)


@pytest.fixture
def provider() -> OperatingProvider:
    return OperatingProvider()


async def test_provider_operations_are_invoked_through_the_host(
    provider: OperatingProvider,
) -> None:
    assert isinstance(provider, ProvidesOperations)
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    uri = "echo:/provider-ops"
    try:
        await open_session(wire, uri)
        publisher = provider.contexts[0].publisher
        assert publisher is not None
        channel = await publisher.changes_published(
            Changeset(label="c", operations=(ChangesetOperation("test.op", "Do"),)), [_EDIT]
        )
        await wire.request("subscribe", {"channel": channel})
        response = await wire.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "test.op"}
        )
        assert "error" not in response
        assert len(provider.calls) == 1
        assert provider.calls[0][:2] == (channel, "test.op")
    finally:
        await shut(host, wire, serving)


async def test_provider_operations_duplicate_id_fails(provider: OperatingProvider) -> None:
    host = Host(provider, LoopbackSingleUserPolicy())
    try:
        with pytest.raises(ValueError, match=r"test\.op") as raised:
            host.register_operation("test.op", provider._handler)
        assert provider.agent.provider in str(raised.value)
        # An id the embedder registered itself may still be replaced.
        host.register_operation("mine", provider._handler)
        host.register_operation("mine", provider._handler)
    finally:
        await host.aclose()

    other = OperatingProvider("other")
    with pytest.raises(ValueError, match=r"test\.op"):
        Host([provider, other], LoopbackSingleUserPolicy())
