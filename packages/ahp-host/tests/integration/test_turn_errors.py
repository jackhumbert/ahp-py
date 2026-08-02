"""What a failed turn puts on the wire.

`ErrorInfo.required` is `['errorType', 'message']` and we sent only `message`,
so VS Code rendered every failure as the literal string
`Error: (undefined) <message>`: its first mapper wants
`_meta.chatError.fetchError.type` and returns undefined without it, so the
fallback template `Error: ({0}) {1}` always won with `errorType` interpolated
as `undefined`.

247/247 reducer fixtures were green throughout, because the official fixture
hands the reducer a ready-made `{"errorType": "runtime", ...}` -- it proves we
CONSUME the field, never that we produce it. So these tests start a real turn
against a provider that fails and assert on the frame the sequencer published.
That distinction is the whole point of the file.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.provider import EchoProvider
from agent_host_server.provider.base import AgentSessionContext, TurnSink, UserMessage

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Exploding(EchoProvider):
    """A provider whose turn raises, which is the common real failure."""

    async def create_session(self, context: AgentSessionContext) -> Any:
        session = await super().create_session(context)

        async def boom(message: UserMessage, sink: TurnSink) -> None:
            raise RuntimeError("the model exploded")

        session.send_user_message = boom  # type: ignore[method-assign]
        return session


class Declining(EchoProvider):
    """A provider that fails politely through the sink instead."""

    async def create_session(self, context: AgentSessionContext) -> Any:
        session = await super().create_session(context)

        async def decline(message: UserMessage, sink: TurnSink) -> None:
            await sink.turn_failed("provider said no", "provider.declined")

        session.send_user_message = decline  # type: ignore[method-assign]
        return session


async def _run_turn(host: Host, uri: str) -> list[dict[str, Any]]:
    client_transport, server_transport = memory_pair()
    serve = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    try:
        await client.request(
            "initialize",
            {
                "channel": ROOT_URI,
                "clientId": "c1",
                "protocolVersions": ["0.7.0"],
                "initialSubscriptions": [ROOT_URI],
            },
        )
        await client.request("createSession", {"channel": uri, "provider": "echo"})
        await client.collect(seconds=0.3)
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        chat = state["chats"][0]["resource"]
        await client.request("subscribe", {"channel": chat})
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 1,
                "action": {
                    "type": "chat/turnStarted",
                    "turnId": "t1",
                    "startedAt": "1970-01-01T00:00:01.000Z",
                    "message": {"text": "go", "origin": {"kind": "user"}},
                },
            },
        )
        await client.collect(seconds=0.8)
        return [a["action"] for a in client.actions(chat)]
    finally:
        serve.cancel()
        await host.aclose()


class TestAFailedTurnCarriesAnErrorType:
    async def test_a_provider_exception(self) -> None:
        actions = await _run_turn(Host(Exploding(), LoopbackSingleUserPolicy()), "echo:/e1")
        errors = [a for a in actions if a["type"] == "chat/error"]
        assert errors, "the turn raised and published no error"
        error = errors[-1]["error"]
        # REQUIRED. Without it the client renders "Error: (undefined) ...".
        assert error["errorType"]
        assert "the model exploded" in error["message"]

    async def test_an_explicit_sink_failure(self) -> None:
        actions = await _run_turn(Host(Declining(), LoopbackSingleUserPolicy()), "echo:/e2")
        error = [a for a in actions if a["type"] == "chat/error"][-1]["error"]
        assert error["errorType"] == "provider.declined"
        assert error["message"] == "provider said no"

    async def test_the_default_matches_the_reference_hosts_vocabulary(self) -> None:
        """Any non-empty string renders -- the schema has no enum -- so the
        default follows the reference host (`agent.turn`) rather than the
        `somethingFailed` style VS Code's own host emits and nothing reads."""
        actions = await _run_turn(Host(Exploding(), LoopbackSingleUserPolicy()), "echo:/e3")
        assert [a for a in actions if a["type"] == "chat/error"][-1]["error"][
            "errorType"
        ] == "agent.turn"


class TestDurationIsMeasured:
    async def test_a_completed_turn_reports_elapsed_time(self) -> None:
        """It was hardcoded 0, and the client renders it as `elapsedMs`, so
        every turn in the transcript looked instantaneous."""
        host = Host(EchoProvider(delay=0.12), LoopbackSingleUserPolicy())
        actions = await _run_turn(host, "echo:/d1")
        complete = [a for a in actions if a["type"] == "chat/turnComplete"]
        assert complete, "the turn never completed"
        assert complete[-1]["duration"] > 0

    async def test_a_failed_turn_reports_elapsed_time_too(self) -> None:
        actions = await _run_turn(Host(Exploding(), LoopbackSingleUserPolicy()), "echo:/d2")
        assert [a for a in actions if a["type"] == "chat/error"][-1]["duration"] >= 0
