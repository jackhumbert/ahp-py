"""`MessageChatAttachment`: pinning `endTurn`, and supplying the transcript.

"When `endTurn` is omitted, the host MUST resolve and pin the referenced chat's
latest completed turn when accepting the message ... The host MUST reject an
attachment that references an unknown chat, specifies an unknown, active, or
non-retained `endTurn`" (`chat-channel.md`, Pulling a chat into another chat;
`MessageChatAttachment`). The host did none of it: the attachment passed
through untouched, so its bound moved with every later turn of the chat it
named, and the agent was never given the transcript at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import AgentSessionContext, TurnSink, UserMessage

from .hosting import (
    Wire,
    assert_frames_valid,
    connect,
    finished,
    open_session,
    run_turn,
    shut,
    state,
    turn_started,
)

pytestmark = pytest.mark.anyio

_CHATS: dict[str, Any] = {"multipleChats": {}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Recording(EchoSession):
    def __init__(self, context: AgentSessionContext, delay: float = 0.0) -> None:
        super().__init__(context, delay=delay)
        self.messages: list[UserMessage] = []

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self.messages.append(message)
        await super().send_user_message(message, sink)


class RecordingProvider(EchoProvider):
    def __init__(self, delay: float = 0.0) -> None:
        super().__init__(capabilities=_CHATS)
        self.sessions: list[Recording] = []
        self._session_delay = delay

    async def create_session(self, context: AgentSessionContext) -> Recording:
        session = Recording(context, delay=self._session_delay)
        self.sessions.append(session)
        return session


def _attaching(chat: str, **extra: Any) -> dict[str, Any]:
    return {"type": "chat", "resource": chat, "label": "Side chat", **extra}


async def _with_history(wire: Wire, uri: str) -> tuple[str, str]:
    """A session whose default chat has two completed turns; and a second chat."""
    default = await open_session(wire, uri)
    await run_turn(wire, default, "a1", "first")
    await run_turn(wire, default, "a2", "second")
    other = f"ahp-chat:/{uri.rsplit('/', 1)[-1]}-other"
    response = await wire.request("createChat", {"channel": uri, "chat": other})
    assert "error" not in response, response
    await wire.request("subscribe", {"channel": other})
    return default, other


async def test_an_omitted_end_turn_is_pinned_and_the_transcript_supplied() -> None:
    provider = RecordingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default, other = await _with_history(wire, "echo:/att-1")
        seq = await wire.dispatch(
            other, turn_started("b1", "summarise that", attachments=[_attaching(default)])
        )
        echo = await wire.echoed(other, seq)
        assert "rejectionReason" not in echo
        # Pinned in the ACCEPTED action, so every client's transcript holds
        # the bound -- not just the host's memory.
        [pinned] = echo["action"]["message"]["attachments"]
        assert pinned["endTurn"] == "a2"
        assert pinned["label"] == "Side chat"
        assert await wire.until(lambda: finished(host, other, "b1"))
        [stored] = state(host, other)["turns"][-1]["message"]["attachments"]
        assert stored["endTurn"] == "a2"

        message = provider.sessions[0].messages[-1]
        [attached] = message.attached_chats
        assert (attached.resource, attached.end_turn, attached.label) == (
            default,
            "a2",
            "Side chat",
        )
        assert [t["id"] for t in attached.turns] == ["a1", "a2"]
        assert_frames_valid(wire, ("chat", other))
    finally:
        await shut(host, wire, serving)


async def test_a_given_end_turn_bounds_the_transcript() -> None:
    provider = RecordingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default, other = await _with_history(wire, "echo:/att-2")
        await wire.dispatch(
            other,
            turn_started("b1", "only the first", attachments=[_attaching(default, endTurn="a1")]),
        )
        assert await wire.until(lambda: finished(host, other, "b1"))
        [attached] = provider.sessions[0].messages[-1].attached_chats
        assert [t["id"] for t in attached.turns] == ["a1"]
    finally:
        await shut(host, wire, serving)


@pytest.mark.parametrize(
    ("attachment", "reason"),
    [
        (
            {"type": "chat", "resource": "ahp-chat:/nowhere", "label": "?"},
            "a chat attachment names an unknown chat",
        ),
        (None, "a chat attachment's endTurn is not a completed turn of that chat"),
    ],
)
async def test_an_unresolvable_attachment_is_rejected(
    attachment: dict[str, Any] | None, reason: str
) -> None:
    provider = RecordingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default, other = await _with_history(wire, "echo:/att-3")
        chosen = attachment if attachment is not None else _attaching(default, endTurn="zz")
        seq = await wire.dispatch(other, turn_started("b1", "x", attachments=[chosen]))
        assert (await wire.echoed(other, seq))["rejectionReason"] == reason
        assert state(host, other).get("activeTurn") is None
    finally:
        await shut(host, wire, serving)


async def test_the_active_turn_is_not_a_completed_one() -> None:
    provider = RecordingProvider(delay=0.4)
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/att-4"
        default = await open_session(wire, uri)
        await wire.request("createChat", {"channel": uri, "chat": "ahp-chat:/att-4-b"})
        await wire.request("subscribe", {"channel": "ahp-chat:/att-4-b"})
        await wire.dispatch(default, turn_started("a1"))
        assert await wire.until(lambda: state(host, default).get("activeTurn") is not None)
        seq = await wire.dispatch(
            "ahp-chat:/att-4-b",
            turn_started("b1", "x", attachments=[_attaching(default, endTurn="a1")]),
        )
        echo = await wire.echoed("ahp-chat:/att-4-b", seq)
        assert echo["rejectionReason"] == (
            "a chat attachment's endTurn is not a completed turn of that chat"
        )
    finally:
        await shut(host, wire, serving)


async def test_a_chat_with_no_completed_turn_is_accepted_and_empty() -> None:
    """ "the resolved transcript is empty and the host MUST NOT reject the
    attachment on that basis"."""
    provider = RecordingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/att-5"
        default = await open_session(wire, uri)
        await wire.request("createChat", {"channel": uri, "chat": "ahp-chat:/att-5-b"})
        seq = await wire.dispatch(
            default, turn_started("a1", "x", attachments=[_attaching("ahp-chat:/att-5-b")])
        )
        echo = await wire.echoed(default, seq)
        assert "rejectionReason" not in echo
        assert "endTurn" not in echo["action"]["message"]["attachments"][0]
        assert await wire.until(lambda: finished(host, default, "a1"))
        [attached] = provider.sessions[0].messages[-1].attached_chats
        assert attached.end_turn is None
        assert attached.turns == ()
    finally:
        await shut(host, wire, serving)


async def test_a_queued_message_is_pinned_when_queued_not_when_run() -> None:
    """ "Later turns do not change the context represented by an already-sent
    attachment" -- the bound is taken at acceptance."""
    provider = RecordingProvider(delay=0.3)
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default, other = await _with_history(wire, "echo:/att-6")
        await wire.dispatch(other, turn_started("b1", "busy"))
        assert await wire.until(lambda: state(host, other).get("activeTurn") is not None)
        seq = await wire.dispatch(
            other,
            {
                "type": "chat/pendingMessageSet",
                "kind": "queued",
                "id": "q1",
                "message": {
                    "text": "then this",
                    "origin": {"kind": "user"},
                    "attachments": [_attaching(default)],
                },
            },
        )
        echo = await wire.echoed(other, seq)
        assert echo["action"]["message"]["attachments"][0]["endTurn"] == "a2"
        # A later turn on the referenced chat does not move the bound.
        await run_turn(wire, default, "a3", "third")
        assert await wire.until(lambda: len(state(host, other).get("turns", [])) == 2)
        [attached] = provider.sessions[0].messages[-1].attached_chats
        assert [t["id"] for t in attached.turns] == ["a1", "a2"]
    finally:
        await shut(host, wire, serving)


async def test_create_chat_applies_the_same_rules_to_its_initial_message() -> None:
    provider = RecordingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/att-7"
        default, _ = await _with_history(wire, uri)
        refused = await wire.request(
            "createChat",
            {
                "channel": uri,
                "chat": "ahp-chat:/att-7-bad",
                "initialMessage": {
                    "text": "x",
                    "origin": {"kind": "user"},
                    "attachments": [_attaching(default, endTurn="zz")],
                },
            },
        )
        assert refused["error"]["code"] == -32602
        assert not host.sequencer.has_channel("ahp-chat:/att-7-bad")

        created = await wire.request(
            "createChat",
            {
                "channel": uri,
                "chat": "ahp-chat:/att-7-good",
                "initialMessage": {
                    "text": "pull it in",
                    "origin": {"kind": "user"},
                    "attachments": [_attaching(default)],
                },
            },
        )
        assert "error" not in created, created
        assert await wire.until(lambda: bool(state(host, "ahp-chat:/att-7-good").get("turns")))
        [stored] = state(host, "ahp-chat:/att-7-good")["turns"][0]["message"]["attachments"]
        assert stored["endTurn"] == "a2"
    finally:
        await shut(host, wire, serving)
