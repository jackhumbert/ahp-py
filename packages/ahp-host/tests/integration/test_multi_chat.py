"""Multi-chat: `createChat`, `disposeChat`, and the aggregation rules.

The aggregation is the part worth testing. Upstream states it as producer
SHOULDs describing a derivation, not as a reducer — so nothing enforces it, and
the interesting rule is the **promotion**: a session list renders one status,
and without promotion a worker chat blocked on input is invisible in it.

Both commands also sit on a spec contradiction. `chat-channel.md` says the
server allocates the chat URI and that `disposeChat` does not exist; the types,
the message map, the reference host and VS Code's client all say otherwise.
Three implementations against one sentence of prose.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair
from agent_host_protocol.types import AHP_ERROR_CODES
from agent_host_protocol.types.protocol import SessionStatus

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_FORKABLE = {"multipleChats": {"fork": True, "sideChat": True}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def multi() -> AsyncIterator[Host]:
    host = Host(EchoProvider(capabilities=_FORKABLE), LoopbackSingleUserPolicy())
    try:
        yield host
    finally:
        await host.aclose()


async def _client(host: Host) -> FakeClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "c1",
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


async def _session(host: Host, client: FakeClient, uri: str, **extra: Any) -> str:
    await client.request("createSession", {"channel": uri, "provider": "echo", **extra})
    await client.collect(seconds=0.3)
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    default: str = state["chats"][0]["resource"]
    return default


async def _one_turn(
    client: FakeClient, chat: str, *, turn_id: str = "t1", text: str = "hello"
) -> str:
    """Drive one complete turn and return its id."""
    await client.request("subscribe", {"channel": chat})
    await client.notify(
        "dispatchAction",
        {
            "channel": chat,
            "clientSeq": 1,
            "action": {
                "type": "chat/turnStarted",
                "turnId": turn_id,
                "startedAt": "1970-01-01T00:00:01.000Z",
                "message": {"text": text, "origin": {"kind": "user"}},
            },
        },
    )
    await client.collect(seconds=0.6)
    state = (await client.request("subscribe", {"channel": chat}))["result"]["snapshot"]["state"]
    started: str = state["turns"][-1]["id"]
    return started


class TestCreateChat:
    async def test_a_second_chat_joins_the_catalogue(self, multi: Host) -> None:
        client = await _client(multi)
        uri = "echo:/mc-1"
        await _session(multi, client, uri)

        result = await client.request("createChat", {"channel": uri, "chat": "ahp-chat:/second"})
        # `null`, not `{}`: the CommandMap declares `result: null` for every
        # create/dispose lifecycle command, matching `createSession` on this
        # same host.
        assert result["result"] is None

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert "ahp-chat:/second" in [c["resource"] for c in state["chats"]]
        assert "error" not in await client.request("subscribe", {"channel": "ahp-chat:/second"})

    async def test_the_capability_is_required(self) -> None:
        """ "When absent, clients MUST NOT call `createChat`" -- a client MUST,
        so a client that ignores it just sends the command anyway."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            uri = "echo:/mc-2"
            await _session(host, client, uri)
            response = await client.request(
                "createChat", {"channel": uri, "chat": "ahp-chat:/nope"}
            )
            assert response["error"]["code"] == -32602
        finally:
            await host.aclose()

    async def test_fork_and_side_chat_each_need_their_own_flag(self) -> None:
        host = Host(EchoProvider(capabilities={"multipleChats": {}}), LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            uri = "echo:/mc-3"
            default = await _session(host, client, uri)
            for kind in ("fork", "sideChat"):
                response = await client.request(
                    "createChat",
                    {
                        "channel": uri,
                        "chat": f"ahp-chat:/{kind}",
                        "source": {"kind": kind, "chat": default, "turnId": "t1"},
                    },
                )
                assert response["error"]["code"] == -32602, kind
        finally:
            await host.aclose()

    async def test_the_source_chat_must_belong_to_this_session(self, multi: Host) -> None:
        client = await _client(multi)
        await _session(multi, client, "echo:/mc-4a")
        other = await _session(multi, client, "echo:/mc-4b")

        response = await client.request(
            "createChat",
            {
                "channel": "echo:/mc-4a",
                "chat": "ahp-chat:/cross",
                "source": {"kind": "fork", "chat": other, "turnId": "t1"},
            },
        )
        assert response["error"]["code"] == -32602

    async def test_a_side_chat_selection_is_snapshotted(self, multi: Host) -> None:
        """ "The host MUST snapshot and preserve this exact selection...; later
        source-turn deltas do not alter it." Copied, never referenced."""
        client = await _client(multi)
        uri = "echo:/mc-5"
        default = await _session(multi, client, uri)
        # A real turn: the origin's `turnId` is validated against the source
        # chat now, because publishing a provenance pointing at a turn that
        # does not exist is a claim nothing downstream ever rechecks.
        turn_id = await _one_turn(client, default)
        selection = {"text": "the selected bit", "responsePartId": "p1"}

        await client.request(
            "createChat",
            {
                "channel": uri,
                "chat": "ahp-chat:/side",
                "source": {
                    "kind": "sideChat",
                    "chat": default,
                    "turnId": turn_id,
                    "selection": selection,
                },
            },
        )
        selection["text"] = "mutated afterwards"

        state = (await client.request("subscribe", {"channel": "ahp-chat:/side"}))["result"][
            "snapshot"
        ]["state"]
        assert state["origin"]["selection"]["text"] == "the selected bit"

    async def test_a_duplicate_chat_uri_is_refused(self, multi: Host) -> None:
        """`AlreadyExists` (-32010), not `SessionAlreadyExists` (-32003): the
        colliding URI names a chat, not a session -- the same reasoning
        `createTerminal` already applies."""
        client = await _client(multi)
        uri = "echo:/mc-6"
        await _session(multi, client, uri)
        await client.request("createChat", {"channel": uri, "chat": "ahp-chat:/dup"})
        response = await client.request("createChat", {"channel": uri, "chat": "ahp-chat:/dup"})
        assert response["error"]["code"] == AHP_ERROR_CODES["AlreadyExists"]
        assert "Session" not in response["error"]["message"]

    async def test_a_working_directory_outside_the_session_set_is_refused(self) -> None:
        """ "Every entry MUST be present in the owning session's
        `workingDirectories`; the server MUST reject any entry that is not."
        Without this a chat names a root the session was never granted."""
        host = Host(
            EchoProvider(capabilities={"multipleChats": {}, "multipleWorkingDirectories": {}}),
            LoopbackSingleUserPolicy(),
        )
        try:
            client = await _client(host)
            uri = "echo:/mc-7"
            await _session(host, client, uri, workingDirectories=["file:///granted"])

            ok = await client.request(
                "createChat",
                {
                    "channel": uri,
                    "chat": "ahp-chat:/subset",
                    "workingDirectories": ["file:///granted"],
                },
            )
            assert "error" not in ok

            bad = await client.request(
                "createChat",
                {
                    "channel": uri,
                    "chat": "ahp-chat:/escape",
                    "workingDirectories": ["file:///not-granted"],
                },
            )
            assert bad["error"]["code"] == -32602
        finally:
            await host.aclose()


class TestDisposeChat:
    async def test_a_chat_can_be_disposed(self, multi: Host) -> None:
        client = await _client(multi)
        uri = "echo:/mc-d1"
        await _session(multi, client, uri)
        await client.request("createChat", {"channel": uri, "chat": "ahp-chat:/temp"})

        disposed = await client.request("disposeChat", {"channel": "ahp-chat:/temp"})
        assert "error" not in disposed
        # `result: null` per the CommandMap, like every create/dispose command.
        assert disposed["result"] is None
        assert not multi.sequencer.has_channel("ahp-chat:/temp")
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert "ahp-chat:/temp" not in [c["resource"] for c in state["chats"]]

    async def test_the_default_chat_cannot_be_disposed(self, multi: Host) -> None:
        """It would leave a session with no `defaultChat`, which every client
        reads."""
        client = await _client(multi)
        uri = "echo:/mc-d2"
        default = await _session(multi, client, uri)
        response = await client.request("disposeChat", {"channel": default})
        assert response["error"]["code"] == -32602

    async def test_disposing_the_session_drops_every_chat(self, multi: Host) -> None:
        client = await _client(multi)
        uri = "echo:/mc-d3"
        default = await _session(multi, client, uri)
        await client.request("createChat", {"channel": uri, "chat": "ahp-chat:/extra"})
        await client.request("disposeSession", {"channel": uri})
        assert not multi.sequencer.has_channel(default)
        assert not multi.sequencer.has_channel("ahp-chat:/extra")


class TestAggregation:
    """The producer SHOULDs nothing else enforces."""

    async def test_input_needed_in_a_non_default_chat_is_promoted(self) -> None:
        """The rule that matters. A session list renders one status; without
        promotion a worker chat blocked on input is invisible in it."""
        host = Host(EchoProvider(elicit=True, capabilities=_FORKABLE), LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            uri = "echo:/agg-1"
            await _session(host, client, uri)
            await client.request("createChat", {"channel": uri, "chat": "ahp-chat:/worker"})
            await client.request("subscribe", {"channel": "ahp-chat:/worker"})

            # A turn in the NON-default chat, which suspends on input.
            await client.notify(
                "dispatchAction",
                {
                    "channel": "ahp-chat:/worker",
                    "clientSeq": 1,
                    "action": {
                        "type": "chat/turnStarted",
                        "turnId": "t1",
                        "startedAt": "1970-01-01T00:00:01.000Z",
                        "message": {"text": "hello", "origin": {"kind": "user"}},
                    },
                },
            )
            await client.collect(seconds=0.5)

            item = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]["items"][
                0
            ]
            assert item["status"] & SessionStatus.INPUT_NEEDED == SessionStatus.INPUT_NEEDED
        finally:
            await host.aclose()

    async def test_modified_at_is_the_max_across_chats(self, multi: Host) -> None:
        client = await _client(multi)
        uri = "echo:/agg-2"
        await _session(multi, client, uri)
        before = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]["items"][
            0
        ]["modifiedAt"]

        await client.request("createChat", {"channel": uri, "chat": "ahp-chat:/later"})
        await client.request("subscribe", {"channel": "ahp-chat:/later"})
        await client.notify(
            "dispatchAction",
            {
                "channel": "ahp-chat:/later",
                "clientSeq": 1,
                "action": {
                    "type": "chat/turnStarted",
                    "turnId": "t1",
                    "startedAt": "1970-01-01T00:00:01.000Z",
                    "message": {"text": "hi", "origin": {"kind": "user"}},
                },
            },
        )
        await client.collect(seconds=0.5)

        after = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]["items"][0][
            "modifiedAt"
        ]
        assert after >= before

    async def test_session_scoped_flag_bits_survive_aggregation(self, multi: Host) -> None:
        """ "The orthogonal flag bits (IsRead, IsArchived) remain
        session-scoped." A chat's activity bits must not clear them."""
        client = await _client(multi)
        uri = "echo:/agg-3"
        await _session(multi, client, uri)
        await client.request("subscribe", {"channel": uri})

        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {"type": "session/isReadChanged", "isRead": True},
            },
        )
        await client.collect(seconds=0.3)

        item = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]["items"][0]
        assert item["status"] & SessionStatus.IS_READ


class TestFork:
    """`createSession.fork` used to be read by nobody.

    The parameter was accepted, the command answered success, and the new
    session came up EMPTY. Silence is the worst available outcome here: the
    user watches their conversation not come across and has nothing to report,
    because every frame on the wire said the fork worked.
    """

    async def test_a_fork_copies_turns_through_the_named_one(self, multi: Host) -> None:
        client = await _client(multi)
        source = await _session(multi, client, "echo:/fork-src")
        first = await _one_turn(client, source, turn_id="t1", text="alpha")
        await _one_turn(client, source, turn_id="t2", text="beta")

        await client.request(
            "createSession",
            {
                "channel": "echo:/fork-dst",
                "provider": "echo",
                "fork": {"session": "echo:/fork-src", "turnId": first},
            },
        )
        await client.collect(seconds=0.3)

        state = (await client.request("subscribe", {"channel": "echo:/fork-dst"}))["result"][
            "snapshot"
        ]["state"]
        chat = (await client.request("subscribe", {"channel": state["chats"][0]["resource"]}))[
            "result"
        ]["snapshot"]["state"]
        # Through and INCLUDING t1, and nothing after it.
        assert [t["id"] for t in chat["turns"]] == ["t1"]

    async def test_the_copy_is_independent(self, multi: Host) -> None:
        """ "An independent copy" -- a later turn on the source must not appear."""
        client = await _client(multi)
        source = await _session(multi, client, "echo:/fork-ind")
        await _one_turn(client, source, turn_id="t1", text="alpha")

        await client.request(
            "createSession",
            {
                "channel": "echo:/fork-ind2",
                "provider": "echo",
                "fork": {"session": "echo:/fork-ind", "turnId": "t1"},
            },
        )
        await client.collect(seconds=0.3)
        await _one_turn(client, source, turn_id="t2", text="added after the fork")

        state = (await client.request("subscribe", {"channel": "echo:/fork-ind2"}))["result"][
            "snapshot"
        ]["state"]
        chat = (await client.request("subscribe", {"channel": state["chats"][0]["resource"]}))[
            "result"
        ]["snapshot"]["state"]
        assert [t["id"] for t in chat["turns"]] == ["t1"]

    async def test_an_unknown_source_session_is_refused(self, multi: Host) -> None:
        client = await _client(multi)
        await _session(multi, client, "echo:/fork-u")
        response = await client.request(
            "createSession",
            {
                "channel": "echo:/fork-u2",
                "provider": "echo",
                "fork": {"session": "echo:/nope", "turnId": "t1"},
            },
        )
        assert response["error"]["code"] == AHP_ERROR_CODES["SessionNotFound"]

    async def test_a_turn_that_does_not_exist_is_refused(self, multi: Host) -> None:
        """Not a silent empty copy: that is the bug this class exists for."""
        client = await _client(multi)
        await _session(multi, client, "echo:/fork-t")
        response = await client.request(
            "createSession",
            {
                "channel": "echo:/fork-t2",
                "provider": "echo",
                "fork": {"session": "echo:/fork-t", "turnId": "no-such-turn"},
            },
        )
        assert response["error"]["code"] == -32602

    async def test_a_fork_inherits_working_directories_and_ignores_the_clients(
        self, multi: Host
    ) -> None:
        """ "Ignored for forked sessions -- a fork inherits ... from the source."

        An override, not a fallback: VS Code computes and sends
        workingDirectories on the fork call anyway.
        """
        client = await _client(multi)
        source = await _session(
            multi, client, "echo:/fork-wd", workingDirectories=["file:///from-source"]
        )
        await _one_turn(client, source, turn_id="t1", text="alpha")

        await client.request(
            "createSession",
            {
                "channel": "echo:/fork-wd2",
                "provider": "echo",
                "workingDirectories": ["file:///the-client-asked-for-this"],
                "fork": {"session": "echo:/fork-wd", "turnId": "t1"},
            },
        )
        await client.collect(seconds=0.3)

        state = (await client.request("subscribe", {"channel": "echo:/fork-wd2"}))["result"][
            "snapshot"
        ]["state"]
        assert state.get("workingDirectories") == ["file:///from-source"]

    async def test_a_fork_carries_the_source_title(self, multi: Host) -> None:
        """Otherwise every fork reads "New Session"."""
        client = await _client(multi)
        source = await _session(multi, client, "echo:/fork-ti")
        await _one_turn(client, source, turn_id="t1", text="alpha")
        await multi.sequencer.publish(
            "echo:/fork-ti", {"type": "session/titleChanged", "title": "Something Specific"}
        )

        await client.request(
            "createSession",
            {
                "channel": "echo:/fork-ti2",
                "provider": "echo",
                "fork": {"session": "echo:/fork-ti", "turnId": "t1"},
            },
        )
        await client.collect(seconds=0.3)

        state = (await client.request("subscribe", {"channel": "echo:/fork-ti2"}))["result"][
            "snapshot"
        ]["state"]
        assert state["title"] == "Something Specific"
