"""Several clients over one session — the thing AHP exists for.

Everything else in this suite tests one client. But the protocol's whole reason
to exist is that an editor, a browser tab and a CLI can attach to the same live
session and any of them can answer a prompt. That is where a host's ordering,
fan-out and arbitration are actually load-bearing, and none of it is exercised
by a single connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.channels import ROOT_URI
from agent_host_server.provider import EchoProvider
from agent_host_server.transport import memory_pair

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Peers:
    def __init__(self, host: Host) -> None:
        self.host = host
        self.clients: list[FakeClient] = []
        self._tasks: list[asyncio.Task[None]] = []

    async def add(self, client_id: str) -> FakeClient:
        client_transport, server_transport = memory_pair()
        self._tasks.append(asyncio.create_task(self.host.serve(server_transport)))
        client = FakeClient(client_transport)
        await client.request(
            "initialize",
            {
                "channel": ROOT_URI,
                "clientId": client_id,
                "protocolVersions": ["0.7.0"],
                "initialSubscriptions": [ROOT_URI],
            },
        )
        self.clients.append(client)
        return client

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.host.aclose()


@pytest.fixture
async def peers() -> AsyncIterator[Peers]:
    group = Peers(Host(EchoProvider(), LoopbackSingleUserPolicy()))
    try:
        yield group
    finally:
        await group.aclose()


async def _shared_session(peers: Peers, uri: str) -> tuple[FakeClient, FakeClient, str]:
    """Two clients attached to one session and its chat."""
    alice = await peers.add("alice")
    bob = await peers.add("bob")

    await alice.request("createSession", {"channel": uri, "provider": "echo"})
    await alice.collect(seconds=0.3)

    state = (await alice.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat_uri = state["chats"][0]["resource"]

    await bob.request("subscribe", {"channel": uri})
    for client in (alice, bob):
        await client.request("subscribe", {"channel": chat_uri})
    return alice, bob, chat_uri


class TestFanOut:
    async def test_both_clients_see_the_same_envelopes_in_the_same_order(
        self, peers: Peers
    ) -> None:
        uri = "echo:/multi-1"
        alice, bob, chat_uri = await _shared_session(peers, uri)

        await alice.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 1,
                "action": {
                    "type": "chat/turnStarted",
                    "turnId": "t1",
                    "startedAt": "1970-01-01T00:00:01.000Z",
                    "message": {"text": "hello", "origin": {"kind": "user"}},
                },
            },
        )
        await asyncio.gather(alice.collect(seconds=0.5), bob.collect(seconds=0.5))

        alice_seqs = [a["serverSeq"] for a in alice.actions(chat_uri)]
        bob_seqs = [a["serverSeq"] for a in bob.actions(chat_uri)]
        assert alice_seqs == bob_seqs, "clients disagree on the action stream"
        assert alice_seqs == sorted(alice_seqs), "envelopes arrived out of order"

    async def test_a_late_subscriber_gets_a_snapshot_it_can_continue_from(
        self, peers: Peers
    ) -> None:
        """The snapshot plus everything after `fromSeq` must equal the full history."""
        uri = "echo:/multi-2"
        alice, _bob, chat_uri = await _shared_session(peers, uri)
        await alice.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 1,
                "action": {
                    "type": "chat/turnStarted",
                    "turnId": "t1",
                    "startedAt": "1970-01-01T00:00:01.000Z",
                    "message": {"text": "hi", "origin": {"kind": "user"}},
                },
            },
        )
        await alice.collect(seconds=0.5)

        carol = await peers.add("carol")
        snapshot = (await carol.request("subscribe", {"channel": chat_uri}))["result"]["snapshot"]
        assert snapshot["state"]["turns"], "late subscriber got a snapshot with no history"
        # Nothing already folded into the snapshot may also arrive as an action.
        await carol.collect(seconds=0.2)
        for envelope in carol.actions(chat_uri):
            assert envelope["serverSeq"] > snapshot["fromSeq"]


class TestOrigin:
    async def test_only_the_originator_is_named_in_origin(self, peers: Peers) -> None:
        """Both clients receive the same envelope; `origin` identifies who sent it,
        which is how a client tells its own optimistic action from a peer's."""
        uri = "echo:/multi-3"
        alice, bob, _chat = await _shared_session(peers, uri)

        await bob.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 7,
                "action": {"type": "session/titleChanged", "title": "Bob was here"},
            },
        )
        await asyncio.gather(alice.collect(seconds=0.3), bob.collect(seconds=0.3))

        for client in (alice, bob):
            echoes = [
                a for a in client.actions(uri) if a["action"]["type"] == "session/titleChanged"
            ]
            assert echoes, "an action was not fanned out to every subscriber"
            assert echoes[0]["origin"] == {"clientId": "bob", "clientSeq": 7}


class TestArbitration:
    async def test_the_host_sequences_concurrent_dispatches_from_two_clients(
        self, peers: Peers
    ) -> None:
        """Whatever the interleaving, every client sees one total order."""
        uri = "echo:/multi-4"
        alice, bob, _chat = await _shared_session(peers, uri)

        await asyncio.gather(
            *(
                client.notify(
                    "dispatchAction",
                    {
                        "channel": uri,
                        "clientSeq": index,
                        "action": {"type": "session/titleChanged", "title": f"{name}-{index}"},
                    },
                )
                for name, client in (("alice", alice), ("bob", bob))
                for index in range(5)
            )
        )
        await asyncio.gather(alice.collect(seconds=0.6), bob.collect(seconds=0.6))

        alice_view = [(a["serverSeq"], a["action"].get("title")) for a in alice.actions(uri)]
        bob_view = [(a["serverSeq"], a["action"].get("title")) for a in bob.actions(uri)]
        assert alice_view == bob_view, "clients disagree on the total order"
        seqs = [s for s, _ in alice_view]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs), "a serverSeq was handed out twice"

    async def test_both_clients_converge_on_the_same_final_state(self, peers: Peers) -> None:
        """The point of host-authoritative sequencing: everyone ends up equal."""
        uri = "echo:/multi-5"
        alice, bob, _chat = await _shared_session(peers, uri)
        await asyncio.gather(
            *(
                client.notify(
                    "dispatchAction",
                    {
                        "channel": uri,
                        "clientSeq": index,
                        "action": {"type": "session/titleChanged", "title": f"{name}-{index}"},
                    },
                )
                for name, client in (("alice", alice), ("bob", bob))
                for index in range(3)
            )
        )
        await asyncio.gather(alice.collect(seconds=0.5), bob.collect(seconds=0.5))

        alice_state = (await alice.request("subscribe", {"channel": uri}))["result"]
        bob_state = (await bob.request("subscribe", {"channel": uri}))["result"]
        assert alice_state["snapshot"]["state"] == bob_state["snapshot"]["state"]


class TestIsolation:
    async def test_one_client_disconnecting_does_not_disturb_the_other(self, peers: Peers) -> None:
        uri = "echo:/multi-6"
        alice, bob, _chat = await _shared_session(peers, uri)
        await bob.transport.close()

        await alice.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {"type": "session/titleChanged", "title": "still here"},
            },
        )
        await alice.collect(seconds=0.3)
        titles = [
            a["action"].get("title")
            for a in alice.actions(uri)
            if a["action"]["type"] == "session/titleChanged"
        ]
        assert "still here" in titles

    async def test_a_bad_frame_from_one_client_does_not_affect_the_other(
        self, peers: Peers
    ) -> None:
        """A malformed action must not take down a shared session.

        Before the sequencer was hardened this both burnt a serverSeq and ended
        the offending client's read loop.
        """
        uri = "echo:/multi-7"
        alice, bob, chat_uri = await _shared_session(peers, uri)

        await bob.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 1,
                # `id` is unhashable; the reference keys a Map with it happily.
                "action": {
                    "type": "chat/pendingMessageSet",
                    "kind": "queued",
                    "id": {"nope": True},
                    "message": {"text": "x"},
                },
            },
        )
        await asyncio.gather(alice.collect(seconds=0.3), bob.collect(seconds=0.3))

        # Both connections are still usable.
        for client in (alice, bob):
            assert "error" not in await client.request("ping", {"channel": ROOT_URI})
