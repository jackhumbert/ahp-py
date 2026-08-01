"""Partitioning between users, tested from the attacker's side.

`docs/requests.md` §2 asks for the harness rather than the class, and it is
right: every deployment writes the same four negative tests, and they are the
ones that fail loudly when a future change routes around a hook.

The four: peer B cannot **see** A's session, cannot **subscribe** to its
channels, cannot **dispatch** into it, and cannot **resume A's connection** by
asserting A's `clientId` -- which is the one that catches a policy partitioning
on an identifier the protocol validates nowhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_host_server.core import Host
from agent_host_server.core.channels import ROOT_URI
from agent_host_server.core.policies import OwnedSessionPolicy, principal_from_header
from agent_host_server.provider import EchoProvider
from agent_host_server.transport import memory_pair

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_HEADER = "x-forwarded-user"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def shared() -> AsyncIterator[tuple[Host, OwnedSessionPolicy]]:
    policy = OwnedSessionPolicy(principal_from_header(_HEADER))
    host = Host(EchoProvider(), policy)
    try:
        yield host, policy
    finally:
        await host.aclose()


async def _connect(host: Host, user: str | None, client_id: str) -> FakeClient:
    client_transport, server_transport = memory_pair()
    headers = {_HEADER: user} if user is not None else {}
    task = asyncio.create_task(host.serve(server_transport, peer="proxy", headers=headers))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    return client


async def _initialize(client: FakeClient, client_id: str) -> dict[str, Any]:
    return await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )


async def _own_session(
    host: Host, policy: OwnedSessionPolicy, client: FakeClient, user: str, uri: str
) -> str:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect(seconds=0.3)
    session = host._sessions[uri]
    policy.claim(user, uri, session.chat_uri, session.annotations_uri)
    return session.chat_uri


class TestHeadersReachThePolicy:
    async def test_a_forwarded_header_is_visible_to_authorize_connection(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        """The blocking request. Before this, `ConnectionInfo.headers` was
        never populated by anything -- a hook that looked usable and was not."""
        host, _ = shared
        client = await _connect(host, "alice", "a1")
        assert "error" not in await _initialize(client, "a1")

    async def test_a_peer_with_no_principal_is_refused_at_the_door(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        client = await _connect(host, None, "nobody")
        response = await _initialize(client, "nobody")
        assert response["error"]["code"] == -32009

    async def test_the_connection_token_reaches_the_policy_too(self) -> None:
        """`?tkn=` was checked for equality at the door and then discarded."""
        seen: list[str | None] = []

        class Recording:
            def authorize_connection(self, info: Any) -> bool:
                seen.append(info.token)
                return True

            def __getattr__(self, _name: str) -> Any:
                return lambda *a, **k: True

        host = Host(EchoProvider(), Recording())
        try:
            client_transport, server_transport = memory_pair()
            task = asyncio.create_task(host.serve(server_transport, token="s3cret"))
            assert task is not None
            client = FakeClient(client_transport)
            await _initialize(client, "c")
            assert seen == ["s3cret"]
        finally:
            await host.aclose()


class TestPartitioning:
    async def test_b_cannot_see_a_session_in_the_catalogue(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        """`listSessions` has no filter parameter, so this can only happen in
        `may_see_channel`."""
        host, policy = shared
        alice = await _connect(host, "alice", "a1")
        await _initialize(alice, "a1")
        await _own_session(host, policy, alice, "alice", "echo:/owned")

        bob = await _connect(host, "bob", "b1")
        await _initialize(bob, "b1")
        listing = (await bob.request("listSessions", {"channel": ROOT_URI}))["result"]
        assert listing["items"] == []

        mine = (await alice.request("listSessions", {"channel": ROOT_URI}))["result"]
        assert [i["resource"] for i in mine["items"]] == ["echo:/owned"]

    async def test_b_cannot_subscribe_to_a_channel(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, policy = shared
        alice = await _connect(host, "alice", "a1")
        await _initialize(alice, "a1")
        chat = await _own_session(host, policy, alice, "alice", "echo:/owned")

        bob = await _connect(host, "bob", "b1")
        await _initialize(bob, "b1")
        for channel in ("echo:/owned", chat, "echo:/owned/annotations"):
            response = await bob.request("subscribe", {"channel": channel})
            assert response["error"]["code"] == -32009, channel

    async def test_b_cannot_dispatch_into_a_session(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        """Seeing and acting are separate hooks, and a peer that guessed a URI
        never had to subscribe first."""
        host, policy = shared
        alice = await _connect(host, "alice", "a1")
        await _initialize(alice, "a1")
        await _own_session(host, policy, alice, "alice", "echo:/owned")
        await alice.request("subscribe", {"channel": "echo:/owned"})

        bob = await _connect(host, "bob", "b1")
        await _initialize(bob, "b1")
        await bob.notify(
            "dispatchAction",
            {
                "channel": "echo:/owned",
                "clientSeq": 1,
                "action": {"type": "session/titleChanged", "title": "owned by bob now"},
            },
        )
        await alice.collect(seconds=0.3)

        state = (await alice.request("subscribe", {"channel": "echo:/owned"}))["result"][
            "snapshot"
        ]["state"]
        assert state["title"] != "owned by bob now"

    async def test_b_cannot_resume_as_a_by_asserting_a_client_id(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        """The one that catches a policy partitioning on `clientId`.

        `reconnect` is a valid first request and "carries no credential: it
        resumes on a client-asserted `clientId` alone". Bob claims Alice's
        `clientId` and asks to resume her subscriptions; the header is what
        decides, so he gets nothing.
        """
        host, policy = shared
        alice = await _connect(host, "alice", "a1")
        await _initialize(alice, "a1")
        chat = await _own_session(host, policy, alice, "alice", "echo:/owned")

        bob = await _connect(host, "bob", "b1")
        result = (
            await bob.request(
                "reconnect",
                {
                    "clientId": "a1",  # Alice's, asserted by Bob
                    "lastSeenServerSeq": 0,
                    "subscriptions": ["echo:/owned", chat],
                },
            )
        )["result"]
        assert result.get("snapshots", []) == []
        assert result.get("actions", []) == []
        assert sorted(result["missing"]) == sorted(["echo:/owned", chat])

    async def test_root_stays_visible_to_everyone(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        """It carries the agent list and the session *count*, not identities."""
        host, policy = shared
        alice = await _connect(host, "alice", "a1")
        await _initialize(alice, "a1")
        await _own_session(host, policy, alice, "alice", "echo:/owned")

        bob = await _connect(host, "bob", "b1")
        await _initialize(bob, "b1")
        assert "error" not in await bob.request("subscribe", {"channel": ROOT_URI})

    async def test_resources_and_root_config_are_refused_by_default(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        """A multi-user host has no single filesystem to expose and no user who
        owns the host-wide settings."""
        host, _ = shared
        alice = await _connect(host, "alice", "a1")
        await _initialize(alice, "a1")
        response = await alice.request(
            "resourceRead", {"channel": ROOT_URI, "uri": "file:///etc/passwd"}
        )
        assert response["error"]["code"] in (-32008, -32009)

    async def test_an_unclaimed_channel_is_refused_not_shared(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        """The failure mode of the other choice is a leak."""
        host, _policy = shared
        alice = await _connect(host, "alice", "a1")
        await _initialize(alice, "a1")
        # Created but deliberately never claimed.
        await alice.request("createSession", {"channel": "echo:/unclaimed", "provider": "echo"})
        await alice.collect(seconds=0.3)
        response = await alice.request("subscribe", {"channel": "echo:/unclaimed"})
        assert response["error"]["code"] == -32009


class TestCounters:
    async def test_the_numbers_are_reachable_without_private_attributes(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        """ "The port is open" is a weak liveness signal for a process whose
        interesting failures all keep the port open."""
        host, policy = shared
        alice = await _connect(host, "alice", "a1")
        await _initialize(alice, "a1")
        await _own_session(host, policy, alice, "alice", "echo:/counted")

        counters = host.counters()
        assert counters["connections"] == 1
        assert counters["sessions"] == 1
        assert counters["pendingRequests"] == 0
        assert counters["serverSeq"] > 0
        assert set(counters) == {
            "connections",
            "sessions",
            "activeTurns",
            "pendingRequests",
            "watches",
            "channels",
            "serverSeq",
        }


class TestAudit:
    """Decisions, retainable, with no conversation content by construction."""

    @staticmethod
    def _sink() -> Any:
        from agent_host_server.core.audit import AuditEvent

        class Recording:
            def __init__(self) -> None:
                self.events: list[AuditEvent] = []

            def record(self, event: AuditEvent) -> None:
                self.events.append(event)

        return Recording()

    async def test_admission_and_refusal_are_both_recorded(self) -> None:
        sink = self._sink()
        policy = OwnedSessionPolicy(principal_from_header(_HEADER))
        host = Host(EchoProvider(), policy, audit=sink)
        try:
            good = await _connect(host, "alice", "a1")
            await _initialize(good, "a1")
            bad = await _connect(host, None, "nobody")
            await _initialize(bad, "nobody")

            kinds = [e.kind for e in sink.events]
            assert "connection.admitted" in kinds
            assert "connection.refused" in kinds
            refused = next(e for e in sink.events if e.kind == "connection.refused")
            assert refused.allowed is False
        finally:
            await host.aclose()

    async def test_a_rejected_action_records_the_reason_but_not_the_payload(self) -> None:
        """The action TYPE, never its contents: a rejected `chat/turnStarted`
        carries the user's message, and an audit record must not."""
        sink = self._sink()
        policy = OwnedSessionPolicy(principal_from_header(_HEADER))
        host = Host(EchoProvider(), policy, audit=sink)
        try:
            alice = await _connect(host, "alice", "a1")
            await _initialize(alice, "a1")
            await _own_session(host, policy, alice, "alice", "echo:/audited")
            bob = await _connect(host, "bob", "b1")
            await _initialize(bob, "b1")
            await bob.notify(
                "dispatchAction",
                {
                    "channel": "echo:/audited",
                    "clientSeq": 1,
                    "action": {"type": "session/titleChanged", "title": "SECRET TITLE"},
                },
            )
            await bob.collect(seconds=0.3)

            rejected = [e for e in sink.events if e.kind == "action.rejected"]
            assert rejected
            assert rejected[-1].reason == "rejected by policy"
            assert rejected[-1].detail == {"action": "session/titleChanged"}
            assert "SECRET TITLE" not in repr(sink.events)
        finally:
            await host.aclose()

    async def test_a_failing_sink_cannot_take_the_host_down(self) -> None:
        """The record is a consequence of the host working, not a condition."""

        class Exploding:
            def record(self, event: Any) -> None:
                raise RuntimeError("audit backend is down")

        policy = OwnedSessionPolicy(principal_from_header(_HEADER))
        host = Host(EchoProvider(), policy, audit=Exploding())
        try:
            alice = await _connect(host, "alice", "a1")
            assert "error" not in await _initialize(alice, "a1")
            await _own_session(host, policy, alice, "alice", "echo:/still-works")
            assert host.counters()["sessions"] == 1
        finally:
            await host.aclose()
