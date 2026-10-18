"""Sequencer redaction: selective delivery, replay truncation, owed snapshots.

Pure sequencer tests -- no host. The reducer is bound to "chat" but only ever
sees unknown actions, which return the state unchanged (invariant 3), so the
state is whatever the test seeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ahp_host.core.changesets import ContentStore
from ahp_host.core.redaction import RedactionError
from ahp_host.core.sequencer import Sequencer

pytestmark = pytest.mark.anyio

CHAT = "chat://one"
ACTION = {"type": "ahp-py/chat/partRedacted", "turnId": "t", "partId": "p"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass(eq=False)
class FakeSubscriber:
    client_id: str
    extensions: frozenset[str] = frozenset()
    messages: list[Any] = field(default_factory=list)

    def enqueue(self, message: Any) -> None:
        self.messages.append(message)


def _transform(state: Any) -> Any:
    return {**state, "redacted": True}


def _deliver(subscriber: Any) -> bool:
    return "ahp-py/redaction" in subscriber.extensions


async def _sequencer() -> Sequencer:
    sequencer = Sequencer()
    await sequencer.register_channel(CHAT, {"turns": []}, "chat")
    return sequencer


async def _noise(sequencer: Sequencer, count: int) -> None:
    for _ in range(count):
        await sequencer.publish(CHAT, {"type": "unknown/noise"})


async def test_sequencer_redaction_delivers_only_to_opted_subscribers() -> None:
    sequencer = await _sequencer()
    opted = FakeSubscriber("a", frozenset({"ahp-py/redaction"}))
    stock = FakeSubscriber("b")
    await sequencer.subscribe(opted, CHAT)
    await sequencer.subscribe(stock, CHAT)
    before = sequencer.server_seq
    envelope = await sequencer.redact(CHAT, ACTION, _transform, deliver=_deliver)
    assert envelope is not None
    assert envelope["serverSeq"] == before + 1
    assert sequencer.server_seq == before + 1
    assert len(opted.messages) == 1
    assert opted.messages[0]["method"] == "action"
    assert opted.messages[0]["params"]["serverSeq"] == envelope["serverSeq"]
    assert "origin" not in envelope
    assert stock.messages == []


async def test_sequencer_redaction_state_is_transformed() -> None:
    sequencer = await _sequencer()
    await sequencer.redact(CHAT, ACTION, _transform, deliver=_deliver)
    assert sequencer.state_of(CHAT) == {"turns": [], "redacted": True}


async def test_sequencer_redaction_truncates_replay_to_a_snapshot() -> None:
    sequencer = await _sequencer()
    opted = FakeSubscriber("a", frozenset({"ahp-py/redaction"}))
    start = sequencer.server_seq
    await _noise(sequencer, 3)
    envelope = await sequencer.redact(CHAT, ACTION, _transform, deliver=_deliver)
    assert envelope is not None
    await _noise(sequencer, 1)
    assert [e["serverSeq"] for e in sequencer._log[CHAT]] == [envelope["serverSeq"] + 1]

    below = await sequencer.replay(start, [CHAT], subscriber=opted)
    assert below["type"] == "snapshot"
    assert below["snapshots"][0]["state"] == {"turns": [], "redacted": True}
    assert below["snapshots"][0]["fromSeq"] >= envelope["serverSeq"]

    at = await sequencer.replay(envelope["serverSeq"], [CHAT], subscriber=opted)
    assert at["type"] == "replay"
    assert [e["serverSeq"] for e in at["actions"]] == [envelope["serverSeq"] + 1]


async def test_sequencer_redaction_owes_stock_client_one_snapshot() -> None:
    sequencer = await _sequencer()
    stock = FakeSubscriber("b")
    await sequencer.subscribe(stock, CHAT)
    await sequencer.redact(CHAT, ACTION, _transform, deliver=_deliver)
    await _noise(sequencer, 2)
    latest = sequencer.server_seq
    first = await sequencer.replay(latest, [CHAT], subscriber=stock)
    assert first["type"] == "snapshot"
    second = await sequencer.replay(latest, [CHAT], subscriber=stock)
    assert second["type"] == "replay"


async def test_sequencer_redaction_subscribe_clears_owed() -> None:
    sequencer = await _sequencer()
    stock = FakeSubscriber("b")
    await sequencer.subscribe(stock, CHAT)
    await sequencer.redact(CHAT, ACTION, _transform, deliver=_deliver)
    assert sequencer._snapshot_owed == {CHAT: {"b": None}}
    await sequencer.subscribe(stock, CHAT)
    assert sequencer._snapshot_owed == {}
    result = await sequencer.replay(sequencer.server_seq, [CHAT], subscriber=stock)
    assert result["type"] == "replay"


async def test_sequencer_redaction_unknown_channel_is_ignored() -> None:
    sequencer = await _sequencer()
    before = sequencer.server_seq
    assert await sequencer.redact("chat://nope", ACTION, _transform, deliver=_deliver) is None
    assert sequencer.server_seq == before


async def test_sequencer_redaction_failing_transform_consumes_nothing() -> None:
    sequencer = await _sequencer()
    await _noise(sequencer, 2)
    before = sequencer.server_seq
    log_before = list(sequencer._log[CHAT])

    def boom(state: Any) -> Any:
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        await sequencer.redact(CHAT, ACTION, boom, deliver=_deliver)
    assert sequencer.server_seq == before
    assert sequencer.state_of(CHAT) == {"turns": []}
    assert list(sequencer._log[CHAT]) == log_before
    assert sequencer._evicted_through.get(CHAT, 0) == 0


async def test_sequencer_redaction_channels_in_registration_order() -> None:
    sequencer = Sequencer()
    await sequencer.register_channel("s://1", {}, "session")
    await sequencer.register_channel("c://1", {"turns": []}, "chat")
    await sequencer.register_channel("c://2", {"turns": []}, "chat")
    assert sequencer.channels() == ["s://1", "c://1", "c://2"]
    assert sequencer.channels("chat") == ["c://1", "c://2"]


async def test_sequencer_redaction_drop_channel_removes_owed() -> None:
    sequencer = await _sequencer()
    stock = FakeSubscriber("b")
    await sequencer.subscribe(stock, CHAT)
    await sequencer.redact(CHAT, ACTION, _transform, deliver=_deliver)
    await sequencer.drop_channel(CHAT)
    assert sequencer._snapshot_owed == {}


def test_sequencer_redaction_content_store_discard() -> None:
    store = ContentStore()
    ref = store.put(b"x" * 10)
    other = store.put(b"y" * 4)
    assert store.size == 14
    assert store.discard(ref["uri"]) is True
    assert store.owns(ref["uri"]) is False
    assert store.size == 4
    assert store.owns(other["uri"]) is True
    assert store.discard(ref["uri"]) is False
    assert store.discard("ahp-content://missing") is False


async def test_sequencer_redaction_no_op_transform_consumes_nothing() -> None:
    sequencer = await _sequencer()
    await _noise(sequencer, 2)
    stock = FakeSubscriber("b")
    opted = FakeSubscriber("a", frozenset({"ahp-py/redaction"}))
    await sequencer.subscribe(stock, CHAT)
    await sequencer.subscribe(opted, CHAT)
    before = sequencer.server_seq
    log_before = list(sequencer._log[CHAT])
    with pytest.raises(RedactionError, match="target not found"):
        await sequencer.redact(CHAT, ACTION, lambda state: state, deliver=_deliver)
    assert sequencer.server_seq == before
    assert list(sequencer._log[CHAT]) == log_before
    assert sequencer._evicted_through.get(CHAT, 0) == 0
    assert sequencer._snapshot_owed == {}
    assert opted.messages == []
    assert stock.messages == []


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_sequencer_owed_entry_expires_after_the_client_leaves() -> None:
    clock = Clock()
    sequencer = Sequencer(owed_ttl=60.0, clock=clock)
    await sequencer.register_channel(CHAT, {"turns": []}, "chat")
    gone, kept = FakeSubscriber("gone"), FakeSubscriber("kept")
    await sequencer.subscribe(gone, CHAT)
    await sequencer.subscribe(kept, CHAT)
    await sequencer.redact(CHAT, ACTION, _transform, deliver=_deliver)
    assert sequencer._snapshot_owed == {CHAT: {"gone": None, "kept": None}}

    await sequencer.unsubscribe_all(gone)
    # Inside the window it is still owed; connected clients never expire.
    assert sequencer._snapshot_owed[CHAT]["gone"] == 1060.0
    clock.now = 1059.0
    await sequencer.replay(sequencer.server_seq, [CHAT], subscriber=kept)
    assert "gone" in sequencer._snapshot_owed[CHAT]

    clock.now = 1061.0
    await sequencer.replay(sequencer.server_seq, [CHAT], subscriber=kept)
    assert "gone" not in sequencer._snapshot_owed.get(CHAT, {})

    # A connected client's debt never expires, however long it lasts.
    other = FakeSubscriber("other")
    await sequencer.subscribe(other, CHAT)
    await sequencer.redact(CHAT, ACTION, _transform, deliver=_deliver)
    clock.now = 10**6
    await sequencer.replay(sequencer.server_seq, [CHAT], subscriber=kept)
    assert sequencer._snapshot_owed[CHAT]["other"] is None


async def test_sequencer_owed_clock_waits_while_another_connection_holds_the_id() -> None:
    clock = Clock()
    sequencer = Sequencer(owed_ttl=60.0, clock=clock)
    await sequencer.register_channel(CHAT, {"turns": []}, "chat")
    first, second = FakeSubscriber("same"), FakeSubscriber("same")
    await sequencer.subscribe(first, CHAT)
    await sequencer.subscribe(second, CHAT)
    await sequencer.redact(CHAT, ACTION, _transform, deliver=_deliver)
    await sequencer.unsubscribe_all(first)
    # `second` still holds the id, so the entry is not on a timer.
    assert sequencer._snapshot_owed == {CHAT: {"same": None}}
    await sequencer.unsubscribe_all(second)
    assert sequencer._snapshot_owed == {CHAT: {"same": 1060.0}}
