"""Operation handlers: the optional `OperationContext` and the `message`/`followUp` result."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.changesets import Changeset, ChangesetOperation, FileChange
from ahp_host.core.host import _takes_context
from ahp_host.provider import EchoProvider, OperationContext
from ahp_host.provider.base import AgentSessionContext
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


class ContextProvider(EchoProvider):
    """Declares `three.op`, `four.op` and `result.op`, recording what each saw."""

    def __init__(self) -> None:
        super().__init__(provider_id="echo")
        self.three: list[tuple[str, str, Mapping[str, Any] | None]] = []
        self.four: list[OperationContext] = []
        self.contexts: list[AgentSessionContext] = []

    async def _three(self, uri: str, operation: str, target: Mapping[str, Any] | None) -> None:
        self.three.append((uri, operation, target))

    async def _four(
        self,
        uri: str,
        operation: str,
        target: Mapping[str, Any] | None,
        context: OperationContext,
    ) -> None:
        self.four.append(context)

    async def _result(
        self,
        uri: str,
        operation: str,
        target: Mapping[str, Any] | None,
        context: OperationContext,
    ) -> Mapping[str, Any]:
        return {
            "message": "done",
            "followUp": {"kind": "uri", "uri": "https://example.com/pr/1"},
            "extra": 1,
        }

    def changeset_operations(self) -> Mapping[str, Any]:
        return {"three.op": self._three, "four.op": self._four, "result.op": self._result}

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        self.contexts.append(context)
        return await super().create_session(context)


class AlicePolicy(LoopbackSingleUserPolicy):
    principal_of = staticmethod(lambda info: "alice")


async def _channel(provider: ContextProvider, wire: Any, uri: str) -> str:
    await open_session(wire, uri)
    publisher = provider.contexts[0].publisher
    assert publisher is not None
    channel = await publisher.changes_published(
        Changeset(
            label="c",
            operations=tuple(
                ChangesetOperation(op, op) for op in ("three.op", "four.op", "result.op")
            ),
        ),
        [_EDIT],
    )
    await wire.request("subscribe", {"channel": channel})
    return channel


async def test_opcontext_three_argument_handler_still_works() -> None:
    provider = ContextProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        channel = await _channel(provider, wire, "echo:/opcontext-three")
        response = await wire.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "three.op"}
        )
        assert response["result"] == {}
        assert provider.three == [(channel, "three.op", None)]
    finally:
        await shut(host, wire, serving)


async def test_opcontext_four_argument_handler_gets_params_and_principal() -> None:
    provider = ContextProvider()
    host = Host(provider, AlicePolicy())
    wire, serving = await connect(host)
    try:
        channel = await _channel(provider, wire, "echo:/opcontext-four")
        base = {"channel": channel, "operationId": "four.op"}
        await wire.request(
            "invokeChangesetOperation", {**base, "_meta": {"ahp-py/operation": {"reason": "no"}}}
        )
        await wire.request("invokeChangesetOperation", base)
        await wire.request(
            "invokeChangesetOperation", {**base, "_meta": {"ahp-py/operation": "nope"}}
        )
        assert provider.four == [
            OperationContext({"reason": "no"}, "alice"),
            OperationContext({}, "alice"),
            OperationContext({}, "alice"),
        ]
    finally:
        await shut(host, wire, serving)


async def test_opcontext_result_is_passed_through() -> None:
    provider = ContextProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        channel = await _channel(provider, wire, "echo:/opcontext-result")
        response = await wire.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "result.op"}
        )
        assert response["result"] == {
            "message": "done",
            "followUp": {"kind": "uri", "uri": "https://example.com/pr/1"},
        }
    finally:
        await shut(host, wire, serving)


def test_opcontext_takes_context_arity() -> None:
    assert not _takes_context(lambda a, b, c: None)
    assert _takes_context(lambda a, b, c, d: None)
    assert _takes_context(lambda *args: None)
    assert not _takes_context(lambda a, b, c, *, d=None: None)
    # No readable signature: taken as the old three-argument shape.
    assert not _takes_context(object())
