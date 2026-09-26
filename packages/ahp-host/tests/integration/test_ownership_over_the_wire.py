"""Partitioning, driven the way a deployment drives it.

`OwnedSessionPolicy` kept a channel-to-principal map that **nothing
populated**. `claim()` had no caller in the library; its only call site was a
test that built sessions in-process and read `session.chat_uri` off the object
it had just constructed. A deployment never holds that object -- `createSession`
arrives over the wire, and `may_create_session` is consulted BEFORE the chat and
annotations channels are minted, so at decision time those URIs do not exist.

So the shipped multi-user example could not do the thing it was an example of,
and its test passed anyway. This file drives it the way a client does: two
connections, two principals, nothing reaching into host internals.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host
from agent_host_server.core.policies import OwnedSessionPolicy
from agent_host_server.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _principal(info: Any) -> str | None:
    """Whoever the bearer token says. A real one would verify it."""
    header = info.headers.get("authorization", "")
    return header.removeprefix("Bearer ") or None


async def _connect(host: Host, principal: str) -> FakeClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(
        host.serve(server_transport, headers={"authorization": f"Bearer {principal}"})
    )
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": principal,
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


@pytest.fixture
async def partitioned() -> Any:
    host = Host(EchoProvider(), OwnedSessionPolicy(_principal))
    try:
        yield host
    finally:
        await host.aclose()


class TestAPeerReachesItsOwnSession:
    async def test_the_creator_can_see_its_own_chat(self, partitioned: Host) -> None:
        """The failure this file exists for. The session was claimable; the
        CHAT the host minted for it was not, so the owner was refused their own
        conversation."""
        alice = await _connect(partitioned, "alice")
        await alice.request("createSession", {"channel": "echo:/a1", "provider": "echo"})
        await alice.collect(seconds=0.3)

        session = await alice.request("subscribe", {"channel": "echo:/a1"})
        assert "error" not in session, session.get("error")
        chat = session["result"]["snapshot"]["state"]["chats"][0]["resource"]

        opened = await alice.request("subscribe", {"channel": chat})
        assert "error" not in opened, opened.get("error")

    async def test_annotations_are_reachable_too(self, partitioned: Host) -> None:
        alice = await _connect(partitioned, "alice")
        await alice.request("createSession", {"channel": "echo:/a2", "provider": "echo"})
        await alice.collect(seconds=0.3)
        opened = await alice.request("subscribe", {"channel": "echo:/a2/annotations"})
        assert "error" not in opened, opened.get("error")


class TestAPeerCannotReachAnother:
    async def test_a_stranger_is_refused_the_session(self, partitioned: Host) -> None:
        alice = await _connect(partitioned, "alice")
        bob = await _connect(partitioned, "bob")
        await alice.request("createSession", {"channel": "echo:/a3", "provider": "echo"})
        await alice.collect(seconds=0.3)

        refused = await bob.request("subscribe", {"channel": "echo:/a3"})
        assert refused["error"]["code"] == -32009

    async def test_a_stranger_is_refused_the_chat(self, partitioned: Host) -> None:
        """The interesting half: guessing the chat URI must not work either."""
        alice = await _connect(partitioned, "alice")
        bob = await _connect(partitioned, "bob")
        await alice.request("createSession", {"channel": "echo:/a4", "provider": "echo"})
        await alice.collect(seconds=0.3)
        chat = (await alice.request("subscribe", {"channel": "echo:/a4"}))["result"]["snapshot"][
            "state"
        ]["chats"][0]["resource"]

        refused = await bob.request("subscribe", {"channel": chat})
        assert refused["error"]["code"] == -32009

    async def test_list_sessions_is_partitioned(self, partitioned: Host) -> None:
        alice = await _connect(partitioned, "alice")
        bob = await _connect(partitioned, "bob")
        await alice.request("createSession", {"channel": "echo:/a5", "provider": "echo"})
        await bob.request("createSession", {"channel": "echo:/b5", "provider": "echo"})
        await alice.collect(seconds=0.3)

        listed = await alice.request("listSessions", {"channel": ROOT_URI})
        resources = {item["resource"] for item in listed["result"]["items"]}
        assert "echo:/a5" in resources
        assert "echo:/b5" not in resources


class TestOwnershipIsReleased:
    async def test_disposing_forgets_the_channels(self, partitioned: Host) -> None:
        """Otherwise the map grows forever, and a re-used URI inherits an owner
        from a session that no longer exists."""
        alice = await _connect(partitioned, "alice")
        await alice.request("createSession", {"channel": "echo:/a6", "provider": "echo"})
        await alice.collect(seconds=0.3)
        chat = (await alice.request("subscribe", {"channel": "echo:/a6"}))["result"]["snapshot"][
            "state"
        ]["chats"][0]["resource"]

        await alice.request("disposeSession", {"channel": "echo:/a6"})
        await alice.collect(seconds=0.3)

        policy = partitioned.policy
        assert policy.owner_of("echo:/a6") is None  # type: ignore[attr-defined]
        assert policy.owner_of(chat) is None  # type: ignore[attr-defined]


class TestOwnershipSurvivesARestart:
    """Durability and partitioning have to compose.

    `StoredSession` carried channels, title, provider and resume state --
    everything except WHO IT BELONGS TO. So a restored session had no owner,
    `may_see_channel` refuses unowned channels, and restoring one produced a
    session nobody could reach, including its author. `may_restore_session`
    refusing by default was a correct answer to a missing capability, not a
    design position.
    """

    async def test_a_restored_session_still_belongs_to_its_owner(self, tmp_path: Any) -> None:
        from agent_host_server.core.store import FileSessionStore

        store_dir = tmp_path / "sessions"

        first = Host(
            EchoProvider(),
            OwnedSessionPolicy(_principal),
            store=FileSessionStore(store_dir),
        )
        try:
            alice = await _connect(first, "alice")
            await alice.request("createSession", {"channel": "echo:/keep", "provider": "echo"})
            await alice.collect(seconds=0.4)
            chat = (await alice.request("subscribe", {"channel": "echo:/keep"}))["result"][
                "snapshot"
            ]["state"]["chats"][0]["resource"]
        finally:
            await first.aclose()

        # A NEW host over the same store -- the restart.
        second = Host(
            EchoProvider(),
            OwnedSessionPolicy(_principal),
            store=FileSessionStore(store_dir),
        )
        try:
            # Explicit: `restore()` is the embedder's call, not something the
            # host does on its own. Nothing in the library invokes it.
            assert await second.restore() == 1
            alice = await _connect(second, "alice")
            reopened = await alice.request("subscribe", {"channel": "echo:/keep"})
            assert "error" not in reopened, reopened.get("error")
            # And the chat, which is the half that has no derivable
            # relationship to the session URI.
            assert "error" not in await alice.request("subscribe", {"channel": chat})

            bob = await _connect(second, "bob")
            refused = await bob.request("subscribe", {"channel": "echo:/keep"})
            assert refused["error"]["code"] == -32009
        finally:
            await second.aclose()

    async def test_a_session_with_no_recorded_owner_is_not_restored(self, tmp_path: Any) -> None:
        """A ghost in everyone's session list is worse than an absence."""
        import json

        from agent_host_server.core.store import FileSessionStore

        store_dir = tmp_path / "sessions"
        store_dir.mkdir(parents=True)
        (store_dir / "ghost.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "uri": "echo:/ghost",
                    "provider": "echo",
                    "createdAt": "1970-01-01T00:00:00.000Z",
                    "channels": {"echo:/ghost": {"provider": "echo", "chats": []}},
                }
            )
        )
        host = Host(
            EchoProvider(),
            OwnedSessionPolicy(_principal),
            store=FileSessionStore(store_dir),
        )
        try:
            assert await host.restore() == 0, "a session with no owner was restored"
            alice = await _connect(host, "alice")
            refused = await alice.request("subscribe", {"channel": "echo:/ghost"})
            assert "error" in refused
        finally:
            await host.aclose()
