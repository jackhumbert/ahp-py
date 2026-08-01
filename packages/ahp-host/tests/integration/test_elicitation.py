"""Elicitation end to end: the agent stops, a human answers, the turn resumes.

This is the first consumer of ADR 0005, and the reason the ADR exists is the
last test in this file: the answer arrives on a *different connection* from the
one that started the turn. That is not an edge case, it is the thing AHP is for,
and it is why the primitive cannot be a return value from anything the caller
holds.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

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


@pytest.fixture
async def elicit() -> AsyncIterator[Host]:
    host = Host(EchoProvider(elicit=True), LoopbackSingleUserPolicy())
    try:
        yield host
    finally:
        await host.aclose()


async def _attach(host: Host, client_id: str) -> FakeClient:
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


async def _open_chat(client: FakeClient, uri: str) -> str:
    """Create a session and subscribe to its chat, without starting a turn."""
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect(seconds=0.3)
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat_uri: str = state["chats"][0]["resource"]
    await client.request("subscribe", {"channel": chat_uri})
    return chat_uri


async def _send(client: FakeClient, chat_uri: str) -> None:
    await client.notify(
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


async def _start_turn(client: FakeClient, uri: str) -> str:
    chat_uri = await _open_chat(client, uri)
    await _send(client, chat_uri)
    return chat_uri


def _open_request(client: FakeClient, chat_uri: str) -> dict[str, Any] | None:
    for envelope in client.actions(chat_uri):
        if envelope["action"]["type"] == "chat/inputRequested":
            request: dict[str, Any] = envelope["action"]["request"]
            return request
    return None


class TestElicitation:
    async def test_the_agent_suspends_and_publishes_a_request(self, elicit: Host) -> None:
        client = await _attach(elicit, "solo")
        chat_uri = await _start_turn(client, "echo:/elicit-1")
        await client.collect(seconds=0.4)

        request = _open_request(client, chat_uri)
        assert request is not None, "the provider never published its input request"
        assert request["questions"][0]["id"] == "style"
        # The id is the host's, not the provider's (ADR 0005 decision 2).
        assert request["id"].startswith("input-")

        # And the turn is still open, waiting.
        state = (await client.request("subscribe", {"channel": chat_uri}))["result"]["snapshot"][
            "state"
        ]
        assert state["activeTurn"] is not None

    async def test_answering_resumes_the_turn_with_the_answer(self, elicit: Host) -> None:
        client = await _attach(elicit, "solo")
        chat_uri = await _start_turn(client, "echo:/elicit-2")
        await client.collect(seconds=0.4)
        request = _open_request(client, chat_uri)
        assert request is not None

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/inputCompleted",
                    "requestId": request["id"],
                    "response": "accept",
                    "answers": {
                        "style": {"kind": "selected", "state": "submitted", "value": "shout"}
                    },
                },
            },
        )
        await client.collect(seconds=0.5)

        deltas = "".join(
            envelope["action"].get("content", "")
            for envelope in client.actions(chat_uri)
            if envelope["action"]["type"] == "chat/delta"
        )
        assert "HELLO" in deltas, f"the answer never reached the provider: {deltas!r}"

    async def test_declining_is_delivered_as_an_outcome_not_a_cancellation(
        self, elicit: Host
    ) -> None:
        client = await _attach(elicit, "solo")
        chat_uri = await _start_turn(client, "echo:/elicit-3")
        await client.collect(seconds=0.4)
        request = _open_request(client, chat_uri)
        assert request is not None

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/inputCompleted",
                    "requestId": request["id"],
                    "response": "decline",
                },
            },
        )
        await client.collect(seconds=0.5)
        deltas = "".join(
            envelope["action"].get("content", "")
            for envelope in client.actions(chat_uri)
            if envelope["action"]["type"] == "chat/delta"
        )
        assert "(cancelled)" in deltas

    async def test_cancelling_the_turn_frees_the_suspended_provider(self, elicit: Host) -> None:
        """The leak ADR 0005 exists to prevent: without a turn-scoped registry
        the provider stays blocked forever and the chat never leaves
        InputNeeded."""
        client = await _attach(elicit, "solo")
        chat_uri = await _start_turn(client, "echo:/elicit-4")
        await client.collect(seconds=0.4)
        assert len(elicit.pending) == 1

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {"type": "chat/turnCancelled", "turnId": "t1"},
            },
        )
        await client.collect(seconds=0.4)
        assert len(elicit.pending) == 0, "a cancelled turn left its request parked"

    async def test_an_unknown_request_id_is_rejected_and_echoed(self, elicit: Host) -> None:
        """ "Servers SHOULD reject client-dispatched input actions when no
        unresolved input-request part has the matching requestId." The reducers
        check none of this."""
        client = await _attach(elicit, "solo")
        chat_uri = await _start_turn(client, "echo:/elicit-5")
        await client.collect(seconds=0.4)

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/inputCompleted",
                    "requestId": "input-never-issued",
                    "response": "accept",
                },
            },
        )
        await client.collect(seconds=0.3)
        echoes = [
            envelope
            for envelope in client.actions(chat_uri)
            if envelope["action"]["type"] == "chat/inputCompleted"
        ]
        assert echoes, "a rejected action MUST still be echoed so the client reverts"
        assert "rejectionReason" in echoes[-1]
        assert len(elicit.pending) == 1, "the real request was resolved by a forged id"

    async def test_a_peer_answers_a_turn_it_did_not_start(self, elicit: Host) -> None:
        """The reason this is a registry and not a return value.

        Alice sends the message; Bob answers the question. Neither the provider
        nor the sink ever sees a connection.
        """
        alice = await _attach(elicit, "alice")
        bob = await _attach(elicit, "bob")

        # Both attached before the turn, which is the shape AHP is for: two
        # front-ends on one live session.
        chat_uri = await _open_chat(alice, "echo:/elicit-6")
        await bob.request("subscribe", {"channel": chat_uri})
        await _send(alice, chat_uri)
        await asyncio.gather(alice.collect(seconds=0.4), bob.collect(seconds=0.4))

        request = _open_request(bob, chat_uri)
        assert request is not None, "the peer never saw the open request"

        await bob.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 1,
                "action": {
                    "type": "chat/inputCompleted",
                    "requestId": request["id"],
                    "response": "accept",
                    "answers": {
                        "style": {"kind": "selected", "state": "submitted", "value": "shout"}
                    },
                },
            },
        )
        await asyncio.gather(alice.collect(seconds=0.5), bob.collect(seconds=0.5))

        deltas = "".join(
            envelope["action"].get("content", "")
            for envelope in alice.actions(chat_uri)
            if envelope["action"]["type"] == "chat/delta"
        )
        assert "HELLO" in deltas, "the originating client never saw the resumed turn"

    async def test_a_late_subscriber_sees_the_open_request_in_its_snapshot(
        self, elicit: Host
    ) -> None:
        """ "Every subscriber to the chat sees open requests and synchronized
        answer drafts in their original response-stream position."

        A client that connects mid-elicitation -- or reconnects across one --
        gets the request from state, not from the action it missed. That is the
        same path a reconnecting VS Code takes.
        """
        alice = await _attach(elicit, "alice")
        chat_uri = await _start_turn(alice, "echo:/elicit-7")
        await alice.collect(seconds=0.4)

        latecomer = await _attach(elicit, "latecomer")
        snapshot = (await latecomer.request("subscribe", {"channel": chat_uri}))["result"][
            "snapshot"
        ]["state"]
        parts = snapshot["activeTurn"]["responseParts"]
        pending = [p for p in parts if p["kind"] == "inputRequest" and "response" not in p]
        assert pending, "a client joining mid-elicitation cannot see what is being asked"
        assert pending[0]["request"]["questions"][0]["id"] == "style"

    async def test_answer_drafts_are_synchronized_between_clients(self, elicit: Host) -> None:
        """ "A user can answer one question on client A and another on client B;
        every subscriber observes the merged answers map." """
        alice = await _attach(elicit, "alice")
        bob = await _attach(elicit, "bob")
        chat_uri = await _open_chat(alice, "echo:/elicit-8")
        await bob.request("subscribe", {"channel": chat_uri})
        await _send(alice, chat_uri)
        await asyncio.gather(alice.collect(seconds=0.4), bob.collect(seconds=0.4))
        request = _open_request(bob, chat_uri)
        assert request is not None

        # Bob drafts an answer; Alice accepts without supplying one.
        await bob.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 1,
                "action": {
                    "type": "chat/inputAnswerChanged",
                    "requestId": request["id"],
                    "questionId": "style",
                    "answer": {"kind": "selected", "state": "submitted", "value": "shout"},
                },
            },
        )
        await asyncio.gather(alice.collect(seconds=0.3), bob.collect(seconds=0.3))
        await alice.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/inputCompleted",
                    "requestId": request["id"],
                    "response": "accept",
                },
            },
        )
        await alice.collect(seconds=0.5)

        deltas = "".join(
            envelope["action"].get("content", "")
            for envelope in alice.actions(chat_uri)
            if envelope["action"]["type"] == "chat/delta"
        )
        assert "HELLO" in deltas, "the draft from the other client was not carried into the outcome"
