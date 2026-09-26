"""Resource watches, end to end.

The properties under test are lifecycle ones, not "does it notice a file". A
watcher that outlives its audience is a background CPU load nobody asked for; a
guessable channel id is a channel every peer can already name; and an
uncoalesced watch turns one `git checkout` into thousands of sequence numbers,
which evicts the channel's own replay history.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.resources import RootedFilesystemResourceProvider
from ahp_host.core.watches import PollingResourceWatcher, WatchRequest
from ahp_host.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def watching(tmp_path: Path) -> AsyncIterator[tuple[Host, Path, PollingResourceWatcher]]:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("one")
    watcher = PollingResourceWatcher(interval=0.05)
    host = Host(
        EchoProvider(),
        LoopbackSingleUserPolicy(),
        resources=RootedFilesystemResourceProvider(root),
        watcher=watcher,
    )
    try:
        yield host, root, watcher
    finally:
        await host.aclose()


async def _attach(host: Host, client_id: str = "c1") -> FakeClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


async def _watch(client: FakeClient, root: Path, **extra: Any) -> str:
    result = (
        await client.request(
            "createResourceWatch", {"channel": ROOT_URI, "uri": root.as_uri(), **extra}
        )
    )["result"]
    channel: str = result["channel"]
    return channel


class TestResourceWatch:
    async def test_the_channel_id_is_not_derivable_from_the_uri(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        """ "Receiver-assigned" is only meaningful if the id is unguessable: a
        watch channel is subscribable by anyone who can name it."""
        host, root, _ = watching
        client = await _attach(host)
        first = await _watch(client, root)
        second = await _watch(client, root)
        assert first != second
        assert root.name not in first
        assert first.startswith("ahp-resource-watch:/")

    async def test_the_state_describes_the_watch(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        """ "The state carries only the descriptor of what is being watched so a
        re-subscribing client can recover the configuration.\""""
        host, root, _ = watching
        client = await _attach(host)
        channel = await _watch(client, root, recursive=True, excludes={"items": ["node_modules"]})
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert state["root"] == root.as_uri()
        assert state["recursive"] is True
        assert state["excludes"] == {"items": ["node_modules"]}

    async def test_a_change_is_reported(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        host, root, _ = watching
        client = await _attach(host)
        channel = await _watch(client, root)
        await client.request("subscribe", {"channel": channel})
        await asyncio.sleep(0.15)

        (root / "b.txt").write_text("new")
        await client.collect(seconds=0.6)

        changes = [
            item
            for envelope in client.actions(channel)
            if envelope["action"]["type"] == "resourceWatch/changed"
            for item in envelope["action"]["changes"]["items"]
        ]
        assert any(c["uri"].endswith("b.txt") and c["type"] == "added" for c in changes)

    async def test_the_change_types_are_the_ones_the_enum_declares(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        """`ResourceChangeType` is closed: `added | updated | deleted`.

        This emitted `created` and `changed`, which are neither, so a conformant
        client saw only deletions and a validating one rejected two thirds of
        the stream. The assertion is written as an equality against the schema's
        own enum rather than a membership check, so a fourth invented value
        fails here too.
        """
        host, root, _ = watching
        (root / "doomed.txt").write_text("x")
        client = await _attach(host)
        channel = await _watch(client, root)
        await client.request("subscribe", {"channel": channel})
        await asyncio.sleep(0.15)

        (root / "b.txt").write_text("new")
        (root / "a.txt").write_text("edited")
        (root / "doomed.txt").unlink()
        await client.collect(seconds=0.6)

        seen = {
            item["type"]
            for envelope in client.actions(channel)
            if envelope["action"]["type"] == "resourceWatch/changed"
            for item in envelope["action"]["changes"]["items"]
        }
        assert seen == {"added", "updated", "deleted"}, seen

    async def test_a_watch_rooted_at_a_single_file_reports_it(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        """`os.walk` yields NOTHING for a file, so this watch was created,
        subscribed and polled forever without reporting anything -- a
        healthy-looking dead watch on a case the spec names explicitly:
        `ResourceWatchState.root` is "for non-recursive watches ... the single
        file or directory"."""
        host, root, _ = watching
        target = root / "a.txt"
        client = await _attach(host)
        result = (
            await client.request(
                "createResourceWatch", {"channel": ROOT_URI, "uri": target.as_uri()}
            )
        )["result"]
        channel = result["channel"]
        await client.request("subscribe", {"channel": channel})
        await asyncio.sleep(0.15)

        target.write_text("mutated")
        await client.collect(seconds=0.6)

        changes = [
            item
            for envelope in client.actions(channel)
            if envelope["action"]["type"] == "resourceWatch/changed"
            for item in envelope["action"]["changes"]["items"]
        ]
        assert changes == [{"uri": target.as_uri(), "type": "updated"}], changes

    async def test_a_burst_is_coalesced_into_few_actions(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        """One action per interval, not one per file. Uncoalesced, a checkout
        would evict this channel's own replay history."""
        host, root, _ = watching
        client = await _attach(host)
        channel = await _watch(client, root)
        await client.request("subscribe", {"channel": channel})
        await asyncio.sleep(0.15)

        for index in range(40):
            (root / f"f{index}.txt").write_text("x")
        await client.collect(seconds=0.8)

        actions = [
            e for e in client.actions(channel) if e["action"]["type"] == "resourceWatch/changed"
        ]
        reported = sum(len(e["action"]["changes"]["items"]) for e in actions)
        assert reported >= 40, "changes were lost"
        assert len(actions) <= 5, f"{reported} changes arrived as {len(actions)} actions"

    async def test_the_watcher_stops_when_the_last_subscriber_leaves(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        """Otherwise a client that walks away leaves a poller running over a
        directory tree for the life of the host."""
        host, root, watcher = watching
        client = await _attach(host)
        channel = await _watch(client, root)
        await client.request("subscribe", {"channel": channel})
        await asyncio.sleep(0.15)
        assert watcher._polls, "the watch never started"

        await client.notify("unsubscribe", {"channel": channel})
        await asyncio.sleep(0.2)
        assert not watcher._polls, "the watcher outlived its audience"
        assert not host.sequencer.has_channel(channel), "the channel outlived the watch"

    async def test_a_watch_nobody_subscribes_to_costs_nothing(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        host, root, watcher = watching
        client = await _attach(host)
        await _watch(client, root)
        await asyncio.sleep(0.2)
        assert not watcher._polls

    async def test_disconnecting_releases_the_watch(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        host, root, watcher = watching
        client = await _attach(host)
        channel = await _watch(client, root)
        await client.request("subscribe", {"channel": channel})
        await asyncio.sleep(0.15)

        await client.transport.close()
        await asyncio.sleep(0.3)
        assert not watcher._polls

    async def test_a_watch_nobody_subscribed_to_is_released_on_disconnect(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        """The leak: `channel_unobserved` was the only release path and it
        cannot fire without a subscriber, so a `createResourceWatch` that was
        never subscribed left the channel registered and a strong reference to
        the closed connection behind -- once per call, unbounded across
        reconnects, since `max_watches_per_connection` gives each new
        connection a fresh allowance.

        "when every subscriber has unsubscribed (or the underlying connection
        drops), the receiver MUST release the watcher."
        """
        host, root, _ = watching
        channels = []
        for index in range(3):
            client = await _attach(host, f"leaker-{index}")
            channels.append(await _watch(client, root))
            await client.transport.close()
            await asyncio.sleep(0.15)

        assert host._watches == {}, "the watches outlived every connection that made them"
        assert not any(host.sequencer.has_channel(c) for c in channels)

    async def test_a_watch_someone_else_is_watching_survives_its_creator(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        """Releasing on the CREATOR's disconnect must not sever a live
        subscriber: the spec keeps the watcher until *every* subscriber has
        gone, and the channel URI is shareable by design."""
        host, root, watcher = watching
        creator = await _attach(host, "creator")
        channel = await _watch(creator, root)
        observer = await _attach(host, "observer")
        await observer.request("subscribe", {"channel": channel})
        await asyncio.sleep(0.15)

        await creator.transport.close()
        await asyncio.sleep(0.2)
        assert channel in host._watches, "the observer's watch was severed"
        assert watcher._polls, "the poller stopped while a subscriber remained"

        await observer.notify("unsubscribe", {"channel": channel})
        await asyncio.sleep(0.2)
        assert host._watches == {}

    async def test_the_per_connection_cap_is_enforced(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        """`createResourceWatch` is unauthenticated beyond the connection, and
        each watch is a standing background cost."""
        host, root, _ = watching
        host._max_watches = 3
        client = await _attach(host)
        for _ in range(3):
            await _watch(client, root)
        response = await client.request(
            "createResourceWatch", {"channel": ROOT_URI, "uri": root.as_uri()}
        )
        assert response["error"]["code"] == -32009

    async def test_a_watch_outside_the_jail_is_refused(
        self, watching: tuple[Host, Path, PollingResourceWatcher]
    ) -> None:
        host, _root, _ = watching
        client = await _attach(host)
        response = await client.request(
            "createResourceWatch", {"channel": ROOT_URI, "uri": "file:///etc"}
        )
        assert response["error"]["code"] in (-32008, -32009)

    async def test_a_host_with_no_watcher_declines(self) -> None:
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host)
            response = await client.request(
                "createResourceWatch", {"channel": ROOT_URI, "uri": "file:///tmp"}
            )
            assert response["error"]["code"] == -32009
        finally:
            await host.aclose()


class TestFilters:
    def test_excludes_prune_whole_directories(self) -> None:
        """Matching each leading directory too, so `node_modules` excludes
        everything beneath it without the caller writing `node_modules/**`."""
        request = WatchRequest(root="file:///x", excludes=("node_modules", "*.log"))
        assert request.matches("src/main.py")
        assert not request.matches("node_modules/pkg/index.js")
        assert not request.matches("debug.log")

    def test_includes_are_a_whitelist_when_present(self) -> None:
        request = WatchRequest(root="file:///x", includes=("src",))
        assert request.matches("src/main.py")
        assert not request.matches("docs/readme.md")

    def test_excludes_beat_includes(self) -> None:
        request = WatchRequest(root="file:///x", includes=("src",), excludes=("src/vendor",))
        assert request.matches("src/main.py")
        assert not request.matches("src/vendor/lib.py")
