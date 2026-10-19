"""Who answered: the policy's `principal_of` on input and confirmation outcomes.

`InputOutcome.answered_by` and `ToolConfirmationOutcome.answered_by` carry the
principal of the connection whose action resolved the request -- never a
clientId, which the client asserts.
"""

from __future__ import annotations

import asyncio

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.policies import principal_from_header
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import (
    AgentSessionContext,
    InputOutcome,
    InputQuestion,
    InputRequest,
    ToolConfirmation,
    ToolConfirmationOutcome,
    TurnSink,
    UserMessage,
)
from ahp_host.provider.echo import EchoSession
from tests.unit.hosting import Wire, finished, state, turn_started

pytestmark = pytest.mark.anyio

_HEADER = "x-forwarded-user"
_URI = "ahp-session:/s1"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class HeaderPolicy(LoopbackSingleUserPolicy):
    principal_of = staticmethod(principal_from_header(_HEADER))


class AskingSession(EchoSession):
    def __init__(self, context: AgentSessionContext, provider: AskingProvider) -> None:
        super().__init__(context)
        self.provider = provider

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self.provider.input = await sink.request_input(
            InputRequest(questions=[InputQuestion("q", "text", "Name?")])
        )
        await sink.tool_call_started("tc-1", "t", {})
        self.provider.confirmation = await sink.confirm_tool_call(
            ToolConfirmation(call_id="tc-1", name="t", invocation_message="run?")
        )
        # An approved call that is never completed keeps the turn open (it
        # never settles into the transcript), as EchoProvider's own tool does.
        await sink.tool_call_completed(
            "tc-1", {"content": [{"type": "text", "text": "ok"}]}, success=True
        )
        await sink.text_delta("done")


class AskingProvider(EchoProvider):
    """Asks one question and one confirmation per turn, recording both outcomes."""

    def __init__(self) -> None:
        super().__init__()
        self.input: InputOutcome | None = None
        self.confirmation: ToolConfirmationOutcome | None = None

    async def create_session(self, context: AgentSessionContext) -> AskingSession:
        return AskingSession(context, self)


async def _connect(host: Host, user: str, client_id: str) -> tuple[Wire, asyncio.Task[None]]:
    client, server = memory_pair()
    serving = asyncio.create_task(host.serve(server, peer="proxy", headers={_HEADER: user}))
    wire = Wire(host, client)
    response = await wire.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["1.0.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    assert "result" in response, response
    wire.initialized = dict(response["result"])
    return wire, serving


async def _run(host: Host, provider: AskingProvider) -> None:
    alice = await _connect(host, "alice", "a1")
    bob = await _connect(host, "bob", "b1")
    try:
        response = await alice[0].request("createSession", {"channel": _URI, "provider": "echo"})
        assert "error" not in response, response
        assert await alice[0].until(lambda: state(host, _URI).get("lifecycle") == "ready")
        for wire, _ in (alice, bob):
            await wire.request("subscribe", {"channel": _URI})
        chat = state(host, _URI)["defaultChat"]
        for wire, _ in (alice, bob):
            await wire.request("subscribe", {"channel": chat})

        await alice[0].dispatch(chat, turn_started("t1"))
        assert await alice[0].until(lambda: bool(state(host, _URI).get("inputNeeded")))
        request_id = state(host, _URI)["inputNeeded"][0]["id"]
        await bob[0].dispatch(
            chat,
            {
                "type": "chat/inputCompleted",
                "requestId": request_id,
                "response": "accept",
                "answers": {"q": {"state": "submitted", "value": "bob"}},
            },
        )
        assert await alice[0].until(lambda: provider.input is not None)
        # The input entry is gone; wait for the tool confirmation to replace it.
        assert await alice[0].until(
            lambda: any(
                e.get("kind") == "toolConfirmation"
                for e in state(host, _URI).get("inputNeeded") or []
            )
        )
        await alice[0].dispatch(
            chat,
            # `turnId` and `confirmed` are required on the wire; without them the
            # host echoes the action with a rejectionReason and the park is
            # never resolved.
            {
                "type": "chat/toolCallConfirmed",
                "turnId": "t1",
                "toolCallId": "tc-1",
                "approved": True,
                "confirmed": "user-action",
            },
        )
        assert await alice[0].until(lambda: finished(host, chat, "t1")), state(host, chat)
    finally:
        for wire, serving in (alice, bob):
            await wire.close()
            serving.cancel()
        await host.aclose()


async def test_answeredby_names_the_answering_principal() -> None:
    provider = AskingProvider()
    await _run(Host(provider, HeaderPolicy()), provider)
    assert provider.input is not None
    assert provider.input.accepted
    assert provider.input.answered_by == "bob"
    assert provider.confirmation is not None
    assert provider.confirmation.approved
    assert provider.confirmation.answered_by == "alice"


async def test_answeredby_none_without_the_hook() -> None:
    provider = AskingProvider()
    await _run(Host(provider, LoopbackSingleUserPolicy()), provider)
    assert provider.input is not None
    assert provider.input.answered_by is None
    assert provider.confirmation is not None
    assert provider.confirmation.answered_by is None
