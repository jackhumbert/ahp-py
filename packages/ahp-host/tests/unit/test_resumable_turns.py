"""A failed turn the agent can pick back up (`chat/turnResume`).

`turn_failed` never set `ErrorResponsePart.resumable` and the host rejected
every `chat/turnResume`, so a turn that died on a rate limit or a dropped
connection could only be retried as a new message -- the partial answer
stranded above it. Now an agent that is `ResumesTurns` may mark its error
resumable, and the host accepts the resume exactly when the spec says a turn
can reopen, and runs the continuation under the same turn id.
"""

from __future__ import annotations

from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import AgentSessionContext, ResumesTurns, TurnSink, UserMessage

from .hosting import (
    Wire,
    assert_frames_valid,
    connect,
    finished,
    open_session,
    shut,
    state,
    turn_started,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Flaky(EchoSession):
    """Fails each first attempt resumably; finishes when resumed."""

    def __init__(self, context: AgentSessionContext, *, resumable: bool = True) -> None:
        super().__init__(context)
        self.resumable = resumable
        self.resumed: list[tuple[str, str]] = []
        self.fail_resume = False

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.text_delta("half an answer")
        await sink.turn_failed("rate limited", "agent.rateLimit", resumable=self.resumable)

    async def resume_turn(self, chat_uri: str, turn_id: str, sink: TurnSink) -> None:
        self.resumed.append((chat_uri, turn_id))
        if self.fail_resume:
            raise RuntimeError("still rate limited")
        await sink.text_delta("and the rest")


class NotResuming(EchoSession):
    """Asks for a resumable error without being able to resume."""

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.turn_failed("rate limited", "agent.rateLimit", resumable=True)


class FlakyProvider(EchoProvider):
    def __init__(self, session_type: type[EchoSession] = Flaky, **options: Any) -> None:
        super().__init__()
        self.sessions: list[EchoSession] = []
        self._type = session_type
        self._options = options

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        session = self._type(context, **self._options)
        self.sessions.append(session)
        return session


def _last(host: Host, chat: str) -> dict[str, Any]:
    turns = state(host, chat).get("turns") or [{}]
    last: dict[str, Any] = turns[-1]
    return last


async def _failed(wire: Wire, uri: str) -> str:
    chat = await open_session(wire, uri)
    await wire.dispatch(chat, turn_started("t1"))
    assert await wire.until(lambda: finished(wire.host, chat, "t1"))
    return chat


def test_the_protocol_is_feature_detected() -> None:
    assert isinstance(Flaky.__new__(Flaky), ResumesTurns)
    assert not isinstance(EchoSession.__new__(EchoSession), ResumesTurns)


async def test_a_resumable_error_is_resumed_under_the_same_turn() -> None:
    provider = FlakyProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await _failed(wire, "echo:/resume-1")
        failed = _last(host, chat)
        assert failed["state"] == "error"
        assert failed["responseParts"][-1]["resumable"] is True

        seq = await wire.dispatch(chat, {"type": "chat/turnResume", "turnId": "t1"})
        assert "rejectionReason" not in await wire.echoed(chat, seq)
        assert await wire.until(lambda: _last(host, chat).get("state") == "complete")

        [agent] = provider.sessions
        assert isinstance(agent, Flaky)
        assert agent.resumed == [(chat, "t1")]
        turn = _last(host, chat)
        assert turn["id"] == "t1"
        assert len(state(host, chat)["turns"]) == 1, "a resume is not a new turn"
        kinds = [p["kind"] for p in turn["responseParts"]]
        # The partial answer, the error it stopped at, then the continuation.
        assert kinds == ["markdown", "error", "markdown"]
        assert turn["responseParts"][-1]["content"] == "and the rest"
        assert_frames_valid(wire, ("chat", chat))
    finally:
        await shut(host, wire, serving)


async def test_a_resume_that_fails_again_ends_the_turn_in_error() -> None:
    provider = FlakyProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await _failed(wire, "echo:/resume-2")
        agent = provider.sessions[0]
        assert isinstance(agent, Flaky)
        agent.fail_resume = True
        await wire.dispatch(chat, {"type": "chat/turnResume", "turnId": "t1"})
        assert await wire.until(lambda: len(_last(host, chat).get("responseParts", [])) == 3)
        turn = _last(host, chat)
        assert turn["state"] == "error"
        error = turn["responseParts"][-1]
        assert "still rate limited" in error["error"]["message"]
        # A raise is not an invitation to try again.
        assert "resumable" not in error
    finally:
        await shut(host, wire, serving)


async def test_an_agent_that_cannot_resume_never_offers_to() -> None:
    """The guard: `resumable=True` from a session that is not `ResumesTurns`
    would put a button on screen that nothing answers."""
    host = Host(FlakyProvider(NotResuming), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await _failed(wire, "echo:/resume-3")
        assert "resumable" not in _last(host, chat)["responseParts"][-1]
        seq = await wire.dispatch(chat, {"type": "chat/turnResume", "turnId": "t1"})
        echo = await wire.echoed(chat, seq)
        assert echo["rejectionReason"] == "this agent cannot resume a failed turn"
    finally:
        await shut(host, wire, serving)


async def test_the_spec_preconditions_are_enforced() -> None:
    """ "The turn MUST be the latest turn, its state MUST be `error`, and its
    final response part MUST be a resumable error." The reducer silently
    no-ops otherwise; a rejection lets the client revert."""
    provider = FlakyProvider(resumable=False)
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await _failed(wire, "echo:/resume-4")
        cases = [
            ("nope", "turnId does not name the chat's latest turn"),
            ("t1", "the latest turn's error is not resumable"),
        ]
        for turn_id, reason in cases:
            seq = await wire.dispatch(chat, {"type": "chat/turnResume", "turnId": turn_id})
            assert (await wire.echoed(chat, seq))["rejectionReason"] == reason
        agent = provider.sessions[0]
        assert isinstance(agent, Flaky)
        assert agent.resumed == []
    finally:
        await shut(host, wire, serving)


async def test_a_completed_turn_cannot_be_resumed() -> None:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/resume-5")
        await wire.dispatch(chat, turn_started("t1"))
        assert await wire.until(lambda: finished(host, chat, "t1"))
        seq = await wire.dispatch(chat, {"type": "chat/turnResume", "turnId": "t1"})
        # The capability is checked first: echo cannot resume anything.
        assert (await wire.echoed(chat, seq))["rejectionReason"] == (
            "this agent cannot resume a failed turn"
        )

        flaky = FlakyProvider()
        other = Host(flaky, LoopbackSingleUserPolicy())
        wire2, serving2 = await connect(other)
        try:
            chat2 = await _failed(wire2, "echo:/resume-6")
            await wire2.dispatch(chat2, {"type": "chat/turnResume", "turnId": "t1"})
            assert await wire2.until(lambda: _last(other, chat2).get("state") == "complete")
            seq = await wire2.dispatch(chat2, {"type": "chat/turnResume", "turnId": "t1"})
            assert (await wire2.echoed(chat2, seq))["rejectionReason"] == (
                "the latest turn did not fail"
            )
        finally:
            await shut(other, wire2, serving2)
    finally:
        await shut(host, wire, serving)
