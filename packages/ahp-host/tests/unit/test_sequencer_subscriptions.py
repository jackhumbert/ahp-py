"""Who `replay` registers, and what it does with a repeated URI.

Both facts are about a `reconnect` frame a peer controls entirely: the
subscription list is peer-supplied, unbounded and not required to be a set.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_host_server.core.sequencer import Sequencer

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Recorder:
    client_id = "recorder"

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def enqueue(self, message: Any) -> None:
        self.messages.append(dict(message))


async def _chat(sequencer: Sequencer, uri: str = "chat://a") -> str:
    await sequencer.register_channel(uri, {"resource": uri, "turns": []}, "chat")
    return uri


class TestRepeatedSubscriptions:
    """N copies of a URI returned N copies of every missed envelope, each with
    the same `serverSeq`. A conformant mirror folds each one, so replayed chat
    text came back multiplied -- and a 50 KB request answered with 62 MB."""

    async def test_a_repeated_uri_replays_each_envelope_once(self) -> None:
        sequencer = Sequencer()
        chat = await _chat(sequencer)
        for index in range(5):
            await sequencer.publish(chat, {"type": "chat/draftChanged", "draft": str(index)})

        once = await sequencer.replay(0, [chat])
        many = await sequencer.replay(0, [chat] * 8)

        assert [a["serverSeq"] for a in many["actions"]] == [
            a["serverSeq"] for a in once["actions"]
        ]

    async def test_a_repeated_uri_is_snapshotted_once(self) -> None:
        """The other branch of the same answer. `reconnect` gets one `type` for
        the whole request, so the duplication had to be shut on both."""
        sequencer = Sequencer(replay_limit=2)
        chat = await _chat(sequencer)
        for index in range(6):
            await sequencer.publish(chat, {"type": "chat/draftChanged", "draft": str(index)})

        result = await sequencer.replay(0, [chat] * 4)
        assert result["type"] == "snapshot"
        assert [s["resource"] for s in result["snapshots"]] == [chat]

    async def test_a_repeated_uri_is_reported_missing_once(self) -> None:
        sequencer = Sequencer()
        result = await sequencer.replay(0, ["gone://", "gone://", "gone://"])
        assert result["missing"] == ["gone://"]


class TestReplayRegistersOnlyWhatItResumes:
    async def test_a_missing_channel_is_not_left_subscribed(self) -> None:
        """The host was telling the client "drop this" and staying subscribed.

        The URI is client-chosen, so the one it was told did not exist may be
        registered later -- by somebody else. The stale subscriber then receives
        another client's traffic on a channel it was told nothing about.
        """
        sequencer = Sequencer()
        recorder = Recorder()

        result = await sequencer.replay(0, ["echo:/not-yet"], subscriber=recorder)
        assert result["missing"] == ["echo:/not-yet"]

        await sequencer.register_channel("echo:/not-yet", {"resource": "echo:/not-yet"}, "session")
        await sequencer.publish("echo:/not-yet", {"type": "session/titleChanged", "title": "hi"})
        assert recorder.messages == []

    async def test_a_resumed_channel_is_subscribed(self) -> None:
        sequencer = Sequencer()
        chat = await _chat(sequencer)
        recorder = Recorder()

        await sequencer.replay(0, [chat], subscriber=recorder)
        await sequencer.publish(chat, {"type": "chat/draftChanged", "draft": "after"})

        assert [m["params"]["action"]["draft"] for m in recorder.messages] == ["after"]

    async def test_registration_and_the_log_read_share_one_critical_section(self) -> None:
        """An envelope is either in `actions` or enqueued -- never both.

        Subscribing first and replaying afterwards delivered anything published
        in between twice; replaying first and subscribing afterwards lost it.
        """
        sequencer = Sequencer()
        chat = await _chat(sequencer)
        await sequencer.publish(chat, {"type": "chat/draftChanged", "draft": "before"})
        recorder = Recorder()

        result = await sequencer.replay(0, [chat], subscriber=recorder)
        await sequencer.publish(chat, {"type": "chat/draftChanged", "draft": "after"})

        replayed = [a["action"]["draft"] for a in result["actions"]]
        enqueued = [m["params"]["action"]["draft"] for m in recorder.messages]
        assert replayed == ["before"]
        assert enqueued == ["after"]


class TestEmptySubscriberSetsAreDropped:
    """A dict entry per URI a peer has ever named, kept forever, is a memory
    footprint the peer chooses the size of."""

    async def test_unsubscribe_removes_the_key(self) -> None:
        sequencer = Sequencer()
        chat = await _chat(sequencer)
        recorder = Recorder()
        await sequencer.subscribe(recorder, chat)
        await sequencer.unsubscribe(recorder, chat)
        assert sequencer.subscriptions_of(recorder) == set()
        assert chat not in sequencer._subscribers

    async def test_unsubscribe_all_removes_the_keys(self) -> None:
        sequencer = Sequencer()
        chat = await _chat(sequencer)
        other = await _chat(sequencer, "chat://b")
        one, two = Recorder(), Recorder()
        for uri in (chat, other):
            await sequencer.subscribe(one, uri)
        await sequencer.subscribe(two, other)

        await sequencer.unsubscribe_all(one)
        assert chat not in sequencer._subscribers
        assert sequencer._subscribers[other] == {two}
