"""Per-chat agents through the gateway (AHP 1.1.0, upstream `c301d494`).

`multipleChatProviders` is advertised only when every connected node advertises
it, like the other handshake extras: a surface that sent `createChat.provider` to a
node without it would be refused there. `createChat` already routes to the session's
node, and `provider` rides along untouched; the merged root `agents` is a union, so
a provider that exists on one node and not another is rejected by the node that is
asked, with `InvalidParams` -- which is correct.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from ahp_client import AhpClient, RpcError
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.provider.echo import EchoProvider

from ahp_gateway.registry import NodeRecord
from tests.fleet import DEV, Fleet, everyone_is_a_dev

pytestmark = pytest.mark.anyio

_MANY: dict[str, Any] = {"multipleChats": {"fork": True, "sideChat": True}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _node(*providers: str, per_chat: bool) -> Host:
    return Host(
        [EchoProvider(provider_id=p, capabilities=_MANY) for p in providers],
        LoopbackSingleUserPolicy(),
        multiple_chat_providers=per_chat,
    )


def _fleet(a: Host, b: Host) -> Fleet:
    return Fleet(
        {"node-a": a, "node-b": b},
        [NodeRecord("node-a", "mem://a", DEV), NodeRecord("node-b", "mem://b", DEV)],
        everyone_is_a_dev,
    )


async def _handshake(fleet: Fleet) -> tuple[AhpClient, dict[str, Any]]:
    raw = AhpClient(fleet.surface_transport())
    await raw.connect()
    return raw, await raw.initialize(client_id="phone")


@pytest.fixture
async def agreed() -> AsyncIterator[Fleet]:
    fleet = _fleet(_node("alpha", "alpha2", per_chat=True), _node("beta", "beta2", per_chat=True))
    yield fleet
    await fleet.aclose()


async def test_the_capability_is_advertised_when_every_node_advertises_it(agreed: Fleet) -> None:
    raw, handshake = await _handshake(agreed)
    assert handshake["multipleChatProviders"] == {}
    await raw.shutdown()


@pytest.mark.parametrize("a_has_it", [True, False])
async def test_one_node_without_it_withholds_it_from_the_surface(a_has_it: bool) -> None:
    fleet = _fleet(_node("alpha", per_chat=a_has_it), _node("beta", per_chat=not a_has_it))
    try:
        raw, handshake = await _handshake(fleet)
        assert "multipleChatProviders" not in handshake
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_no_node_advertising_it_advertises_nothing() -> None:
    fleet = _fleet(_node("alpha", per_chat=False), _node("beta", per_chat=False))
    try:
        raw, handshake = await _handshake(fleet)
        assert "multipleChatProviders" not in handshake
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_provider_reaches_the_sessions_node_untouched(agreed: Fleet) -> None:
    raw, _ = await _handshake(agreed)
    await raw.request("createSession", {"channel": "alpha:/s", "provider": "alpha"})
    await raw.request(
        "createChat", {"channel": "alpha:/s", "chat": "ahp-chat:/other", "provider": "alpha2"}
    )
    state = agreed.hosts["node-a"].sequencer.state_of("ahp-chat:/other")
    assert state["provider"] == "alpha2"
    result, _ = await raw.subscribe("ahp-chat:/other")
    assert result["snapshot"]["state"]["provider"] == "alpha2"
    await raw.shutdown()


async def test_a_provider_the_sessions_node_lacks_is_that_nodes_invalid_params(
    agreed: Fleet,
) -> None:
    """`beta2` is in the merged root's agents, but node-a does not serve it."""
    raw, _ = await _handshake(agreed)
    await raw.request("createSession", {"channel": "alpha:/s", "provider": "alpha"})
    with pytest.raises(RpcError) as caught:
        await raw.request(
            "createChat", {"channel": "alpha:/s", "chat": "ahp-chat:/x", "provider": "beta2"}
        )
    assert caught.value.code == -32602
    assert "beta2" in caught.value.message
    await raw.shutdown()
