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
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.provider import EchoProvider

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


async def _open_chat(host: Host, client: FakeClient, uri: str) -> str:
    """Create a session and subscribe to its chat, without starting a turn."""
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    # The default chat is registered after the response goes out, and the next
    # line indexes `chats[0]`: wait for the entry itself rather than for a fixed
    # moment that was long enough only on an idle machine.
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("chats")),
        timeout=10.0,
    )
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


async def _start_turn(host: Host, client: FakeClient, uri: str) -> str:
    chat_uri = await _open_chat(host, client, uri)
    await _send(client, chat_uri)
    return chat_uri


def _open_request(client: FakeClient, chat_uri: str) -> dict[str, Any] | None:
    for envelope in client.actions(chat_uri):
        if envelope["action"]["type"] == "chat/inputRequested":
            request: dict[str, Any] = envelope["action"]["request"]
            return request
    return None


def _open_part(host: Host, chat_uri: str) -> dict[str, Any] | None:
    """The unresolved input-request part in the chat's live state, if any."""
    active = (host.sequencer.state_of(chat_uri) or {}).get("activeTurn") or {}
    for part in active.get("responseParts", []):
        if part.get("kind") == "inputRequest" and "response" not in part:
            answered: dict[str, Any] = part
            return answered
    return None


async def _asked(client: FakeClient, chat_uri: str, *, timeout: float = 10.0) -> None:
    """Wait until the provider's input request has reached *client*.

    The request id every caller then dispatches against comes off the CLIENT's
    action stream, so this waits for the notification rather than for the host
    state that precedes it -- the state landing first is exactly the race a
    fixed wait papered over.
    """
    await client.collect_until(lambda: _open_request(client, chat_uri) is not None, timeout=timeout)


def _deltas(client: FakeClient, chat_uri: str) -> str:
    return "".join(
        envelope["action"].get("content", "")
        for envelope in client.actions(chat_uri)
        if envelope["action"]["type"] == "chat/delta"
    )


async def _said(client: FakeClient, chat_uri: str, text: str, *, timeout: float = 10.0) -> None:
    """Wait until *text* appears in the deltas *client* has seen on *chat_uri*."""
    await client.collect_until(lambda: text in _deltas(client, chat_uri), timeout=timeout)


class TestElicitation:
    async def test_the_agent_suspends_and_publishes_a_request(self, elicit: Host) -> None:
        client = await _attach(elicit, "solo")
        chat_uri = await _start_turn(elicit, client, "echo:/elicit-1")
        # NOT a condition wait: the second half of this test asserts the turn is
        # STILL OPEN, which is a claim about what did not happen next. Returning
        # the instant the request arrives would let a host that published the
        # request and then ended the turn pass. This one keeps its elapsed wait.
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
        chat_uri = await _start_turn(elicit, client, "echo:/elicit-2")
        await _asked(client, chat_uri)
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
        await _said(client, chat_uri, "HELLO")

        deltas = _deltas(client, chat_uri)
        assert "HELLO" in deltas, f"the answer never reached the provider: {deltas!r}"

    async def test_declining_is_delivered_as_an_outcome_not_a_cancellation(
        self, elicit: Host
    ) -> None:
        client = await _attach(elicit, "solo")
        chat_uri = await _start_turn(elicit, client, "echo:/elicit-3")
        await _asked(client, chat_uri)
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
        await _said(client, chat_uri, "(cancelled)")
        deltas = _deltas(client, chat_uri)
        assert "(cancelled)" in deltas

    async def test_cancelling_the_turn_frees_the_suspended_provider(self, elicit: Host) -> None:
        """The leak ADR 0005 exists to prevent: without a turn-scoped registry
        the provider stays blocked forever and the chat never leaves
        InputNeeded."""
        client = await _attach(elicit, "solo")
        chat_uri = await _start_turn(elicit, client, "echo:/elicit-4")
        await client.collect_until(lambda: len(elicit.pending) == 1, timeout=10.0)
        assert len(elicit.pending) == 1

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {"type": "chat/turnCancelled", "turnId": "t1"},
            },
        )
        # A real transition, not a vacuous one: the assertion above established
        # the count was 1 before the cancel, so reaching 0 can only mean the
        # cancel freed it.
        await client.collect_until(lambda: len(elicit.pending) == 0, timeout=10.0)
        assert len(elicit.pending) == 0, "a cancelled turn left its request parked"

    async def test_an_unknown_request_id_is_rejected_and_echoed(self, elicit: Host) -> None:
        """ "Servers SHOULD reject client-dispatched input actions when no
        unresolved input-request part has the matching requestId." The reducers
        check none of this."""
        client = await _attach(elicit, "solo")
        chat_uri = await _start_turn(elicit, client, "echo:/elicit-5")
        await _asked(client, chat_uri)

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
        # The echo is what proves the forged action was PROCESSED, which is what
        # makes the `pending` count below mean anything: a bare sleep only ever
        # guessed at that.
        await client.collect_until(
            lambda: any(
                envelope["action"]["type"] == "chat/inputCompleted"
                for envelope in client.actions(chat_uri)
            ),
            timeout=10.0,
        )
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
        chat_uri = await _open_chat(elicit, alice, "echo:/elicit-6")
        await bob.request("subscribe", {"channel": chat_uri})
        await _send(alice, chat_uri)
        # Both, because both are subscribed and both connections have to keep
        # draining; the assertion below reads the request off BOB.
        await asyncio.gather(_asked(alice, chat_uri), _asked(bob, chat_uri))

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
        await asyncio.gather(_said(alice, chat_uri, "HELLO"), _said(bob, chat_uri, "HELLO"))

        deltas = _deltas(alice, chat_uri)
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
        chat_uri = await _start_turn(elicit, alice, "echo:/elicit-7")
        # The latecomer reads the request out of SHARED STATE, so that is what
        # this waits for -- the same object the snapshot below is built from.
        await alice.collect_until(lambda: _open_part(elicit, chat_uri) is not None, timeout=10.0)

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
        chat_uri = await _open_chat(elicit, alice, "echo:/elicit-8")
        await bob.request("subscribe", {"channel": chat_uri})
        await _send(alice, chat_uri)
        await asyncio.gather(_asked(alice, chat_uri), _asked(bob, chat_uri))
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

        # Alice's accept below carries no answer of its own, so it may only be
        # dispatched once Bob's draft is IN the merged state -- otherwise the
        # test measures nothing but the ordering of two sleeps.
        def drafted() -> bool:
            request_state = (_open_part(elicit, chat_uri) or {}).get("request") or {}
            return "style" in (request_state.get("answers") or {})

        await asyncio.gather(
            bob.collect_until(drafted, timeout=10.0),
            alice.collect_until(drafted, timeout=10.0),
        )
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
        await _said(alice, chat_uri, "HELLO")

        deltas = _deltas(alice, chat_uri)
        assert "HELLO" in deltas, "the draft from the other client was not carried into the outcome"
