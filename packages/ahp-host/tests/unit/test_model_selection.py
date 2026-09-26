"""The model the user picked reaches the provider.

It arrived on every turn and was dropped, surviving only as an unnamed key
inside `UserMessage.raw` -- so changing the model in the picker APPEARED to
work, because the state round-tripped, and nothing acted on it.

Carried, not obeyed. This host is a courier: it hands the selection to the
provider and never decides what to do with it. Choosing a model from it is the
adapter's business, and routing between models stays out of the host.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import (
    AgentSessionContext,
    ModelSelection,
    UserMessage,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestParsingTheSelection:
    def test_a_selection_with_config_survives(self) -> None:
        selection = ModelSelection.from_wire(
            {"id": "gpt-5", "config": {"thinking": "high", "contextSize": 128000}}
        )
        assert selection is not None
        assert selection.id == "gpt-5"
        # "most pickers produce strings, but some produce numbers or booleans,
        # which are carried through as-is" -- so no coercion.
        assert selection.config == {"thinking": "high", "contextSize": 128000}

    def test_a_bare_selection_has_an_empty_config(self) -> None:
        selection = ModelSelection.from_wire({"id": "echo-1"})
        assert selection is not None
        assert selection.config == {}

    def test_absent_means_the_hosts_default_applies(self) -> None:
        """The spec's own wording, and why this returns None rather than a
        placeholder: an adapter must be able to tell "no pick" from a pick."""
        assert ModelSelection.from_wire(None) is None

    def test_junk_off_the_wire_does_not_become_a_selection(self) -> None:
        """It comes off a client-dispatched action, so it may be any JSON."""
        value: Any
        for value in ({}, {"id": 7}, {"id": None}, [], "gpt-5", 42):
            assert ModelSelection.from_wire(value) is None


class TestItReachesTheProvider:
    async def test_a_turn_carries_the_pick_and_the_agent(self) -> None:
        """End to end through a real turn, because parsing in isolation is
        exactly the kind of test that passes while the field never arrives."""
        seen: list[UserMessage] = []

        class Recording(EchoProvider):
            async def create_session(self, context: AgentSessionContext):  # type: ignore[no-untyped-def]
                session = await super().create_session(context)
                original = session.send_user_message

                async def record(message: UserMessage, sink):  # type: ignore[no-untyped-def]
                    seen.append(message)
                    await original(message, sink)

                session.send_user_message = record  # type: ignore[method-assign]
                return session

        host = Host(Recording(), LoopbackSingleUserPolicy())
        client_transport, server_transport = memory_pair()
        serve = asyncio.create_task(host.serve(server_transport))
        try:
            await client_transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "channel": "ahp-root://",
                        "protocolVersions": ["0.7.0"],
                        "clientId": "c1",
                        "clientInfo": {"name": "t", "version": "0"},
                        "capabilities": {},
                    },
                }
            )
            await client_transport.receive()
            await client_transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "createSession",
                    "params": {"channel": "echo:/m1", "provider": "echo"},
                }
            )
            while True:
                message = await asyncio.wait_for(client_transport.receive(), timeout=5)
                if isinstance(message, dict) and message.get("id") == 2:
                    break
            await asyncio.sleep(0.3)
            chat = host.sequencer.state_of("echo:/m1")["chats"][0]["resource"]
            await client_transport.send(
                {
                    "jsonrpc": "2.0",
                    "method": "dispatchAction",
                    "params": {
                        "channel": chat,
                        "clientSeq": 1,
                        "action": {
                            "type": "chat/turnStarted",
                            "turnId": "t1",
                            "startedAt": "1970-01-01T00:00:01.000Z",
                            "message": {
                                "text": "hi",
                                "origin": {"kind": "user"},
                                "model": {"id": "echo-1", "config": {"thinking": "high"}},
                                "agent": {"uri": "file:///agents/alpha.md"},
                            },
                        },
                    },
                }
            )
            await asyncio.sleep(0.8)
        finally:
            serve.cancel()
            await host.aclose()

        assert seen, "the provider never saw the turn"
        assert seen[0].model is not None
        assert seen[0].model.id == "echo-1"
        assert seen[0].model.config == {"thinking": "high"}
        assert seen[0].agent_uri == "file:///agents/alpha.md"
