"""Per-chat agents (AHP 1.1.0, upstream `c301d494`): the capability and `createChat.provider`.

`Host(multiple_chat_providers=True)` advertises `InitializeResult.multipleChatProviders`
(an opt-in, because it changes what a session persists). Without it `createChat` with a
`provider` is `InvalidParams`.
"""

from __future__ import annotations

from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider

from .hosting import connect, open_session, shut

pytestmark = pytest.mark.anyio

_CHATS: dict[str, Any] = {"multipleChats": {"fork": True, "sideChat": True}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _providers() -> list[EchoProvider]:
    return [
        EchoProvider(capabilities=_CHATS),
        EchoProvider(provider_id="echo2", display_name="Echo 2", capabilities=_CHATS),
    ]


async def test_the_capability_is_absent_unless_the_embedder_opts_in() -> None:
    host = Host(_providers(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        assert "multipleChatProviders" not in wire.initialized
    finally:
        await shut(host, wire, serving)


async def test_the_capability_is_a_presence_only_object_when_enabled() -> None:
    host = Host(_providers(), LoopbackSingleUserPolicy(), multiple_chat_providers=True)
    wire, serving = await connect(host)
    try:
        assert wire.initialized["multipleChatProviders"] == {}
    finally:
        await shut(host, wire, serving)


async def test_create_chat_with_a_provider_is_invalid_params_without_the_capability() -> None:
    host = Host(_providers(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/mp-1")
        response = await wire.request(
            "createChat",
            {"channel": "echo:/mp-1", "chat": "ahp-chat:/mp-1b", "provider": "echo2"},
        )
        assert response["error"]["code"] == -32602, response
        assert "multipleChatProviders" in response["error"]["message"]
        assert not host.sequencer.has_channel("ahp-chat:/mp-1b")
    finally:
        await shut(host, wire, serving)
