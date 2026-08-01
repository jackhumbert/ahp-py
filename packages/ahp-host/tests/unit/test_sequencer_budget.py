"""The replay budget, the eviction watermark, and subscription edges.

The budget is a correctness feature, not a performance one. With one shared
buffer, a terminal streaming output evicts a *chat's* history, and that chat's
next `reconnect` silently degrades to a full snapshot -- or worse, if eviction
went unrecorded, gets told it is up to date across a hole.
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
    """A `Subscriber`, and a `SubscriptionObserver`, in one."""

    client_id = "recorder"

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.edges: list[tuple[str, str]] = []

    def enqueue(self, message: Any) -> None:
        self.messages.append(dict(message))

    def channel_observed(self, channel: str) -> None:
        self.edges.append(("observed", channel))

    def channel_unobserved(self, channel: str) -> None:
        self.edges.append(("unobserved", channel))


async def _noisy(sequencer: Sequencer, channel: str, count: int) -> None:
    for index in range(count):
        await sequencer.publish(channel, {"type": "terminal/data", "data": str(index)})


class TestPerChannelBudget:
    async def test_a_chatty_channel_does_not_evict_a_quiet_one(self) -> None:
        sequencer = Sequencer(replay_limit=4)
        await sequencer.register_channel("quiet", {"turns": []}, "chat")
        await sequencer.register_channel("noisy", {"content": []}, "terminal")

        await sequencer.publish("quiet", {"type": "chat/draftChanged", "draft": "hello"})
        quiet_seq = sequencer.server_seq
        await _noisy(sequencer, "noisy", 50)

        # The quiet channel's one action is still replayable from before it.
        result = await sequencer.replay(quiet_seq - 1, ["quiet"])
        assert result["type"] == "replay"
        assert [a["serverSeq"] for a in result["actions"]] == [quiet_seq]

    async def test_a_channel_that_overflowed_falls_back_to_a_snapshot(self) -> None:
        """Honest, not silent: the alternative is telling a client it is current
        while its state is missing everything the buffer dropped."""
        sequencer = Sequencer(replay_limit=4)
        await sequencer.register_channel("noisy", {"content": []}, "terminal")
        await _noisy(sequencer, "noisy", 50)

        result = await sequencer.replay(1, ["noisy"])
        assert result["type"] == "snapshot"

    async def test_replay_is_ordered_across_channels(self) -> None:
        """Per-channel logs are an implementation detail; the client is promised
        one total order."""
        sequencer = Sequencer(replay_limit=64)
        await sequencer.register_channel("a", {"turns": []}, "chat")
        await sequencer.register_channel("b", {"turns": []}, "chat")
        for index in range(10):
            channel = "a" if index % 2 == 0 else "b"
            await sequencer.publish(channel, {"type": "chat/draftChanged", "draft": str(index)})

        result = await sequencer.replay(0, ["a", "b"])
        seqs = [a["serverSeq"] for a in result["actions"]]
        assert seqs == sorted(seqs)
        assert len(seqs) == 10

    async def test_a_dropped_channel_releases_its_log(self) -> None:
        sequencer = Sequencer(replay_limit=4)
        await sequencer.register_channel("gone", {"turns": []}, "chat")
        await sequencer.publish("gone", {"type": "chat/draftChanged", "draft": "x"})
        await sequencer.drop_channel("gone")

        result = await sequencer.replay(0, ["gone"])
        assert result["missing"] == ["gone"] or result["type"] == "snapshot"


class TestSubscriptionEdges:
    async def test_the_observer_sees_only_the_transitions(self) -> None:
        """A resource watch should not open a filesystem watcher per subscriber,
        nor close one while somebody is still watching."""
        observer = Recorder()
        sequencer = Sequencer(observer=observer)
        await sequencer.register_channel("c", {"turns": []}, "chat")

        first, second = Recorder(), Recorder()
        await sequencer.subscribe(first, "c")
        await sequencer.subscribe(second, "c")
        assert observer.edges == [("observed", "c")]

        await sequencer.unsubscribe(first, "c")
        assert observer.edges == [("observed", "c")], "fired while a subscriber remained"

        await sequencer.unsubscribe(second, "c")
        assert observer.edges == [("observed", "c"), ("unobserved", "c")]

    async def test_unsubscribing_twice_does_not_fire_twice(self) -> None:
        observer = Recorder()
        sequencer = Sequencer(observer=observer)
        subscriber = Recorder()
        await sequencer.subscribe(subscriber, "c")
        await sequencer.unsubscribe(subscriber, "c")
        await sequencer.unsubscribe(subscriber, "c")
        assert observer.edges.count(("unobserved", "c")) == 1

    async def test_disconnecting_fires_every_edge_it_closes(self) -> None:
        observer = Recorder()
        sequencer = Sequencer(observer=observer)
        subscriber = Recorder()
        for channel in ("a", "b"):
            await sequencer.subscribe(subscriber, channel)
        await sequencer.unsubscribe_all(subscriber)
        assert sorted(observer.edges) == [
            ("observed", "a"),
            ("observed", "b"),
            ("unobserved", "a"),
            ("unobserved", "b"),
        ]

    async def test_a_failing_observer_cannot_wedge_the_sequencer(self) -> None:
        """It runs inside the critical section, and it is embedder code."""

        class Exploding:
            def channel_observed(self, channel: str) -> None:
                raise RuntimeError("boom")

            def channel_unobserved(self, channel: str) -> None:
                raise RuntimeError("boom")

        sequencer = Sequencer(observer=Exploding())
        await sequencer.register_channel("c", {"turns": []}, "chat")
        subscriber = Recorder()
        await sequencer.subscribe(subscriber, "c")
        # The lock is still free and the channel still works.
        assert await sequencer.publish("c", {"type": "chat/draftChanged", "draft": "x"})
