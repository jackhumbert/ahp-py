"""Withdrawing a question answered somewhere else, and the sink's turn id.

An agent driven from two places at once asks both. `tool_call_confirmed` let a
provider withdraw an approval prompt the other place answered; nothing did the
same for `request_input`, so after the provider stopped waiting the question
stayed on every client here -- answerable, to no effect -- and the session sat
`InputNeeded` until the turn ended. The provider could not even name the
request: its id is minted by the host.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import (
    AgentSessionContext,
    IdentifiesTurn,
    InputOutcome,
    InputQuestion,
    InputRequest,
    ResolvesInput,
    TurnSink,
    UserMessage,
)

from .hosting import assert_frames_valid, connect, finished, open_session, shut, state, turn_started

pytestmark = pytest.mark.anyio

_ANSWER = {"style": {"state": "submitted", "value": {"kind": "selected", "value": "shout"}}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class AskedTwice(EchoSession):
    """Asks here and "elsewhere"; the test plays the other place."""

    def __init__(self, context: AgentSessionContext) -> None:
        super().__init__(context)
        self.elsewhere = asyncio.Event()
        self.keep_waiting = False
        self.resolved: list[bool] = []
        self.outcomes: list[InputOutcome] = []
        self.turn_ids: list[str] = []

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        assert isinstance(sink, IdentifiesTurn)
        self.turn_ids.append(sink.turn_id)
        ask = asyncio.create_task(
            sink.request_input(
                InputRequest(
                    message="How?",
                    questions=[InputQuestion(id="style", kind="text", message="Style")],
                    key="question-1",
                )
            )
        )
        await self.elsewhere.wait()
        if not self.keep_waiting:
            # What an adapter does when the other place answers first: stop
            # waiting here, then say how it was answered.
            ask.cancel()
        assert isinstance(sink, ResolvesInput)
        self.resolved.append(await sink.input_resolved("question-1", answers=_ANSWER))
        self.resolved.append(await sink.input_resolved("no-such-question"))
        if self.keep_waiting:
            self.outcomes.append(await ask)
        await sink.text_delta("done")


class Provider(EchoProvider):
    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[AskedTwice] = []

    async def create_session(self, context: AgentSessionContext) -> AskedTwice:
        session = AskedTwice(context)
        self.sessions.append(session)
        return session


def _input_part(host: Host, chat: str) -> dict[str, Any]:
    active = state(host, chat).get("activeTurn") or (state(host, chat).get("turns") or [{}])[-1]
    for part in active.get("responseParts", []):
        if part.get("kind") == "inputRequest":
            found: dict[str, Any] = part
            return found
    return {}


def test_the_hosts_sink_is_both() -> None:
    """Separate protocols, so a provider's fake `TurnSink` that predates them
    still is one; the host's own sink implements both."""
    from ahp_host.core.pending import PendingRequests
    from ahp_host.core.sequencer import Sequencer
    from ahp_host.core.turn import ActionTurnSink

    sink = ActionTurnSink(Sequencer(), "ahp-chat:/x", "t9", PendingRequests())
    assert isinstance(sink, ResolvesInput)
    assert isinstance(sink, IdentifiesTurn)
    assert (sink.turn_id, sink.chat_uri) == ("t9", "ahp-chat:/x")


async def test_an_answer_from_elsewhere_withdraws_the_question_here() -> None:
    provider = Provider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    uri = "echo:/input-1"
    try:
        chat = await open_session(wire, uri)
        await wire.dispatch(chat, turn_started("t1"))
        assert await wire.until(lambda: bool(state(host, uri).get("inputNeeded")))
        request_id = _input_part(host, chat)["request"]["id"]

        agent = provider.sessions[0]
        agent.elsewhere.set()
        assert await wire.until(lambda: finished(host, chat, "t1"))
        assert agent.resolved == [True, False]
        assert agent.turn_ids == ["t1"]

        part = _input_part(host, chat)
        # The transcript records how it was settled.
        assert part["response"] == "accept"
        assert part["request"]["answers"] == _ANSWER
        assert not state(host, uri).get("inputNeeded"), "still advertised as waiting"
        completed = wire.actions(chat, "chat/inputCompleted")
        assert [a["requestId"] for a in completed] == [request_id]
        assert_frames_valid(wire, ("chat", chat), ("session", uri))
    finally:
        await shut(host, wire, serving)


async def test_a_request_still_waiting_returns_the_outcome() -> None:
    provider = Provider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/input-2")
        await wire.dispatch(chat, turn_started("t1"))
        assert await wire.until(lambda: bool(_input_part(host, chat)))
        agent = provider.sessions[0]
        agent.keep_waiting = True
        agent.elsewhere.set()
        assert await wire.until(lambda: finished(host, chat, "t1"))
        [outcome] = agent.outcomes
        assert outcome.accepted
        assert outcome.answers == _ANSWER
    finally:
        await shut(host, wire, serving)


async def test_a_client_that_answered_first_wins() -> None:
    provider = Provider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/input-3")
        await wire.dispatch(chat, turn_started("t1"))
        assert await wire.until(lambda: bool(_input_part(host, chat)))
        request_id = _input_part(host, chat)["request"]["id"]
        await wire.dispatch(
            chat, {"type": "chat/inputCompleted", "requestId": request_id, "response": "decline"}
        )
        assert await wire.until(lambda: _input_part(host, chat).get("response") == "decline")
        agent = provider.sessions[0]
        agent.elsewhere.set()
        assert await wire.until(lambda: finished(host, chat, "t1"))
        assert agent.resolved == [False, False]
        assert len(wire.actions(chat, "chat/inputCompleted")) == 1
    finally:
        await shut(host, wire, serving)
