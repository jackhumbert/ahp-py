"""End-to-end host behaviour over the in-memory transport pair.

No sockets, no ports, no model. This is the suite that proves the host obeys the
protocol; the separate interop job proves a real client agrees.
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

pytestmark = pytest.mark.anyio


class FakeClient:
    """A minimal AHP client: enough to drive the host and assert on the wire."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self._next_id = 0
        self.notifications: list[dict[str, Any]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        await self.transport.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            message = await asyncio.wait_for(self.transport.receive(), timeout=5)
            assert message is not None, "transport closed while awaiting a response"
            if message.get("id") == request_id:
                return dict(message)
            self.notifications.append(message)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.transport.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def collect(self, *, seconds: float = 0.2) -> list[dict[str, Any]]:
        """Drain notifications for a moment."""
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                message = await asyncio.wait_for(self.transport.receive(), timeout=remaining)
            except TimeoutError:
                break
            if message is None:
                break
            self.notifications.append(message)
        return self.notifications

    def actions(self, channel: str | None = None) -> list[dict[str, Any]]:
        return [
            n["params"]
            for n in self.notifications
            if n.get("method") == "action"
            and (channel is None or n["params"]["channel"] == channel)
        ]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def connected() -> AsyncIterator[tuple[Host, FakeClient]]:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    client_transport, server_transport = memory_pair()
    serve = asyncio.create_task(host.serve(server_transport))
    try:
        yield host, FakeClient(client_transport)
    finally:
        serve.cancel()
        await host.aclose()


async def _initialize(client: FakeClient, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "channel": ROOT_URI,
        "clientId": "test-client",
        "protocolVersions": ["0.7.0", "0.6.0", "0.5.2", "0.5.1"],
        "initialSubscriptions": [ROOT_URI],
    }
    params.update(overrides)
    return await client.request("initialize", params)


class TestHandshake:
    async def test_negotiates_the_clients_preferred_version(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        response = await _initialize(client)
        result = response["result"]
        assert result["protocolVersion"] == "0.7.0"

    async def test_snapshots_is_always_an_array(self, connected: tuple[Host, FakeClient]) -> None:
        """Omitting it puts MultiHostClient in an endless reconnect loop."""
        _, client = connected
        result = (await _initialize(client, initialSubscriptions=[]))["result"]
        assert result["snapshots"] == []

    async def test_root_snapshot_uses_the_exact_root_uri(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """The client matches 'ahp-root://' with ===, no normalisation."""
        _, client = connected
        result = (await _initialize(client))["result"]
        assert [s["resource"] for s in result["snapshots"]] == [ROOT_URI]
        assert result["snapshots"][0]["state"]["agents"][0]["provider"] == "echo"

    async def test_incompatible_client_is_refused_with_32005(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        response = await _initialize(client, protocolVersions=["0.5.0"])
        assert response["error"]["code"] == -32005
        assert "0.7.0" in response["error"]["data"]["supportedProtocolVersions"]

    async def test_commands_before_initialize_are_refused(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        response = await client.request("listSessions", {"channel": ROOT_URI})
        assert response["error"]["code"] == -32602


class TestUnimplemented:
    async def test_declines_loudly_with_method_not_found(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """No silent stubs: there is no server capability object, so this error
        IS how a host declines a feature."""
        _, client = connected
        await _initialize(client)
        for method in ("createTerminal", "authenticate"):
            response = await client.request(method, {"channel": ROOT_URI})
            assert response["error"]["code"] == -32601, method

    async def test_writes_are_denied_rather_than_unimplemented(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """The write half IS implemented; this host just has no writable
        provider. `PermissionDenied` says that; `MethodNotFound` would claim the
        host cannot write at all, which a client might act on permanently."""
        _, client = connected
        await _initialize(client)
        for method in ("resourceWrite", "resourceDelete", "resourceMkdir"):
            response = await client.request(
                method,
                {"channel": ROOT_URI, "uri": "file:///tmp/x", "data": "", "encoding": "utf-8"},
            )
            assert response["error"]["code"] == -32009, method


class TestSessionLifecycle:
    async def test_create_session_brings_the_session_up(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        session_uri = "ahp-session:/11111111-1111-1111-1111-111111111111"
        response = await client.request(
            "createSession", {"channel": session_uri, "provider": "echo"}
        )
        assert response["result"] is None

        await client.collect()
        methods = [n.get("method") for n in client.notifications]
        assert "root/sessionAdded" in methods

        session_actions = [a["action"]["type"] for a in client.actions()]
        assert "root/activeSessionsChanged" in session_actions

    async def test_list_sessions_returns_items(self, connected: tuple[Host, FakeClient]) -> None:
        """A successful listSessions MUST carry `items`; the client's iteration
        over it sits outside its try/catch."""
        _, client = connected
        await _initialize(client)
        session_uri = "ahp-session:/22222222-2222-2222-2222-222222222222"
        await client.request("createSession", {"channel": session_uri})
        result = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]
        assert [item["resource"] for item in result["items"]] == [session_uri]

    async def test_duplicate_session_is_refused(self, connected: tuple[Host, FakeClient]) -> None:
        _, client = connected
        await _initialize(client)
        uri = "ahp-session:/33333333-3333-3333-3333-333333333333"
        await client.request("createSession", {"channel": uri})
        response = await client.request("createSession", {"channel": uri})
        assert response["error"]["code"] == -32003

    async def test_session_uri_is_opaque_to_the_host(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """Real clients use forms other than ahp-session:/<uuid> -- ahpx sends
        `<provider>:/<uuid>`. A host must never parse the remainder."""
        _, client = connected
        await _initialize(client)
        response = await client.request(
            "createSession", {"channel": "ahp-session:/copilot/weird~id"}
        )
        assert "error" not in response


class TestSequencing:
    async def test_server_seq_is_globally_monotonic_across_channels(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """serverSeq is host-global, not per-channel: reconnect carries one
        scalar covering every subscription."""
        host, client = connected
        await _initialize(client)
        session_uri = "ahp-session:/44444444-4444-4444-4444-444444444444"
        await client.request("createSession", {"channel": session_uri})
        await client.request("subscribe", {"channel": session_uri})
        # Dispatch a session-channel action so the assertion does not race the
        # asynchronous bring-up: a client that subscribes after bring-up has
        # finished correctly sees those actions only in its snapshot.
        await client.notify(
            "dispatchAction",
            {
                "channel": session_uri,
                "clientSeq": 1,
                "action": {"type": "session/titleChanged", "title": "Renamed"},
            },
        )
        await client.collect()

        seqs = [a["serverSeq"] for a in client.actions()]
        assert seqs == sorted(seqs), "envelopes arrived out of order"
        assert len(set(seqs)) == len(seqs), "a serverSeq was reused"
        channels = {a["channel"] for a in client.actions()}
        assert len(channels) > 1, f"expected actions on more than one channel, saw {channels}"

    async def test_client_dispatch_is_echoed_with_a_host_stamped_origin(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """`dispatchAction` carries no clientId, so the host must supply it.

        Getting this wrong silently breaks optimistic reconciliation for every
        client except the originator -- and no client would report it.
        """
        _, client = connected
        await _initialize(client, clientId="stamp-me")
        uri = "ahp-session:/66666666-6666-6666-6666-666666666666"
        await client.request("createSession", {"channel": uri})
        await client.request("subscribe", {"channel": uri})
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 42,
                "action": {"type": "session/titleChanged", "title": "Renamed"},
            },
        )
        await client.collect()
        echoes = [a for a in client.actions(uri) if a["action"]["type"] == "session/titleChanged"]
        assert echoes, "the client's action was never echoed"
        assert echoes[0]["origin"] == {"clientId": "stamp-me", "clientSeq": 42}

    async def test_non_client_dispatchable_action_is_rejected_with_a_reason(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """Only 38 of 85 actions may be client-originated. A rejected action is
        echoed with `rejectionReason` so the client reverts its optimistic
        prediction -- dropping it silently leaves that prediction applied."""
        _, client = connected
        await _initialize(client)
        uri = "ahp-session:/77777777-7777-7777-7777-777777777777"
        await client.request("createSession", {"channel": uri})
        await client.request("subscribe", {"channel": uri})
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                # Host-only: a client may not declare a session ready.
                "action": {"type": "session/ready"},
            },
        )
        await client.collect()
        echoes = [a for a in client.actions(uri) if a["action"]["type"] == "session/ready"]
        assert echoes, "a rejected action must still be echoed"
        assert "not client-dispatchable" in echoes[0]["rejectionReason"]
        # ...and must NOT have been applied: a later subscribe shows the
        # lifecycle untouched by the rejected action.
        fresh = (await client.request("subscribe", {"channel": uri}))["result"]
        assert fresh["snapshot"]["state"]["lifecycle"] in {"creating", "ready"}
        # ...and must NOT have been applied.

    async def test_action_on_an_unknown_channel_is_silently_ignored(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """Specified asymmetry: no echo at all for an unknown channel."""
        _, client = connected
        await _initialize(client)
        await client.notify(
            "dispatchAction",
            {
                "channel": "ahp-session:/does-not-exist",
                "clientSeq": 1,
                "action": {"type": "session/titleChanged", "title": "x"},
            },
        )
        await client.collect(seconds=0.1)
        assert not [a for a in client.actions() if a["channel"] == "ahp-session:/does-not-exist"]

    async def test_subscribe_snapshot_precedes_later_actions(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """Snapshot.fromSeq is the protocol's only formal ordering rule:
        subsequent actions have serverSeq > fromSeq."""
        host, client = connected
        await _initialize(client)
        uri = "ahp-session:/55555555-5555-5555-5555-555555555555"
        await client.request("createSession", {"channel": uri})
        result = (await client.request("subscribe", {"channel": uri}))["result"]
        from_seq = result["snapshot"]["fromSeq"]
        await client.collect()
        for envelope in client.actions(uri):
            assert envelope["serverSeq"] > from_seq


class TestRealClientUriShapes:
    """Session and chat URIs are client-chosen and opaque.

    Captured from a live VS Code 1.131 connection: it uses `<provider>:/<uuid>`
    for sessions and `ahp-chat://<chatId>/<base64 session uri>` for chats --
    neither matches the `ahp-session:` / `ahp-chat:` forms in the spec's
    examples. A host that routes reducers on the URI scheme applies no reducer
    at all: its state freezes at the snapshot while it keeps broadcasting
    actions, and every client diverges with nothing to notice it.
    """

    async def test_provider_scheme_session_uri_is_reduced(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        host, client = connected
        await _initialize(client)
        uri = "echo:/5ae48ebd-4c94-4977-884a-374badc31cd9"
        await client.request("createSession", {"channel": uri, "provider": "echo"})
        await client.request("subscribe", {"channel": uri})
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {"type": "session/titleChanged", "title": "Renamed"},
            },
        )
        await client.collect()
        fresh = (await client.request("subscribe", {"channel": uri}))["result"]
        assert fresh["snapshot"]["state"]["title"] == "Renamed", (
            "the reducer did not run -- state is frozen at the initial snapshot"
        )

    async def test_bring_up_reaches_ready_on_a_provider_scheme_uri(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        uri = "echo:/c0873bda-45aa-4eb0-9e36-6d5ce861fee7"
        await client.request("createSession", {"channel": uri})
        await client.collect()
        fresh = (await client.request("subscribe", {"channel": uri}))["result"]
        state = fresh["snapshot"]["state"]
        assert state["lifecycle"] == "ready"
        assert len(state["chats"]) == 1, "session/chatAdded was not applied"


class TestReconnectAsFirstRequest:
    """VS Code opens with `reconnect`, not `initialize`.

    Its runtime uses `reconnect` whenever it remembers a serverSeq and a
    subscription set. Refusing it does not make VS Code fall back -- it retries
    the same request forever, so the connection never establishes.
    """

    async def test_reconnect_without_a_prior_initialize_is_accepted(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        response = await client.request(
            "reconnect",
            {
                "channel": ROOT_URI,
                "clientId": "098d0783-37bd-47d5-b16e-c232e685c5d1",
                "lastSeenServerSeq": 38,
                "subscriptions": [ROOT_URI],
            },
        )
        assert "error" not in response, response.get("error")
        assert response["result"]["type"] in {"replay", "snapshot"}

    async def test_reconnect_after_a_host_restart_falls_back_to_snapshots(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """A serverSeq from a previous process cannot be replayed."""
        _, client = connected
        result = (
            await client.request(
                "reconnect",
                {
                    "channel": ROOT_URI,
                    "clientId": "c1",
                    "lastSeenServerSeq": 9999,
                    "subscriptions": [ROOT_URI, "echo:/gone", "ahp-chat://default/Z29uZQ"],
                },
            )
        )["result"]
        assert result["type"] in {"replay", "snapshot"}
        # Channels that no longer exist must not appear as snapshots.
        resources = {s["resource"] for s in result.get("snapshots", [])}
        assert "echo:/gone" not in resources

    async def test_the_connection_is_usable_after_a_reconnect_handshake(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await client.request(
            "reconnect",
            {
                "channel": ROOT_URI,
                "clientId": "c1",
                "lastSeenServerSeq": 0,
                "subscriptions": [ROOT_URI],
            },
        )
        listed = await client.request("listSessions", {"channel": ROOT_URI})
        assert "error" not in listed, "reconnect did not establish the connection"


class TestReconnectAcrossAHostRestart:
    """`serverSeq` restarts at 0 when the host does.

    A client that remembers 38 and is told "replay, nothing since 38" believes
    it is up to date while its state is stale and unrecoverable -- strictly
    worse than being told about a gap. A sequence ahead of ours cannot have come
    from this process, so it must force snapshots.

    VS Code hit this on the very first reconnect after a host restart.
    """

    async def test_a_sequence_from_a_previous_epoch_forces_snapshots(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        result = (
            await client.request(
                "reconnect",
                {
                    "channel": ROOT_URI,
                    "clientId": "c1",
                    "lastSeenServerSeq": 38,
                    "subscriptions": [ROOT_URI],
                },
            )
        )["result"]
        assert result["type"] == "snapshot", (
            "a lastSeenServerSeq ahead of ours means a previous host process; "
            "replaying an empty action list would leave the client silently stale"
        )
        assert [s["resource"] for s in result["snapshots"]] == [ROOT_URI]

    async def test_a_sequence_from_this_epoch_still_replays(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        uri = "ahp-session:/88888888-8888-8888-8888-888888888888"
        await client.request("createSession", {"channel": uri})
        await client.collect()
        result = (
            await client.request(
                "reconnect",
                {
                    "channel": ROOT_URI,
                    "clientId": "test-client",
                    "lastSeenServerSeq": 1,
                    "subscriptions": [ROOT_URI],
                },
            )
        )["result"]
        assert result["type"] == "replay"
        assert all(a["serverSeq"] > 1 for a in result["actions"])


class TestTurnCancellation:
    """Any client may cancel a running turn; the host sequences the outcome."""

    @staticmethod
    async def _running_turn(client: FakeClient, uri: str) -> str:
        await _initialize(client)
        await client.request("createSession", {"channel": uri})
        await client.collect(seconds=0.3)
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        chat_uri: str = state["chats"][0]["resource"]
        await client.request("subscribe", {"channel": chat_uri})
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
        return chat_uri

    async def test_cancelling_mid_turn_ends_it_as_cancelled(self) -> None:
        host = Host(EchoProvider(delay=0.25), LoopbackSingleUserPolicy())
        client_transport, server_transport = memory_pair()
        serve = asyncio.create_task(host.serve(server_transport))
        client = FakeClient(client_transport)
        try:
            chat_uri = await self._running_turn(client, "echo:/cancel-1")
            await asyncio.sleep(0.15)
            await client.notify(
                "dispatchAction",
                {
                    "channel": chat_uri,
                    "clientSeq": 2,
                    "action": {"type": "chat/turnCancelled", "turnId": "t1", "duration": 0},
                },
            )
            await client.collect(seconds=0.8)
            state = (await client.request("subscribe", {"channel": chat_uri}))["result"][
                "snapshot"
            ]["state"]
            assert state["turns"][0]["state"] == "cancelled"
            assert state.get("activeTurn") is None

            # A provider returning normally after cancellation must NOT complete
            # the turn: the reducer would no-op, but it still burns a serverSeq
            # and tells every client a cancelled turn finished.
            completes = [
                a for a in client.actions(chat_uri) if a["action"]["type"] == "chat/turnComplete"
            ]
            assert not completes, "a cancelled turn was also completed"
        finally:
            serve.cancel()
            await host.aclose()

    async def test_cancelling_with_no_active_turn_is_rejected(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        uri = "echo:/cancel-2"
        await client.request("createSession", {"channel": uri})
        await client.collect(seconds=0.3)
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        chat_uri = state["chats"][0]["resource"]
        await client.request("subscribe", {"channel": chat_uri})
        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 1,
                "action": {"type": "chat/turnCancelled", "turnId": "nope", "duration": 0},
            },
        )
        await client.collect(seconds=0.3)
        echoes = [
            a for a in client.actions(chat_uri) if a["action"]["type"] == "chat/turnCancelled"
        ]
        assert echoes, "a rejected action must still be echoed"
        assert echoes[0]["rejectionReason"] == "no active turn to cancel"


class TestDisposeSession:
    """ "The server tears down the session backend, drops associated
    subscriptions, and broadcasts `root/sessionRemoved`.\""""

    async def test_disposing_removes_the_channels_and_tells_root(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        uri = "echo:/dispose-1"
        await client.request("createSession", {"channel": uri})
        await client.collect(seconds=0.3)
        chat_uri = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
            "state"
        ]["chats"][0]["resource"]

        assert (await client.request("disposeSession", {"channel": uri}))["result"] is None
        await client.collect(seconds=0.3)

        assert "root/sessionRemoved" in [n.get("method") for n in client.notifications]
        assert (await client.request("listSessions", {"channel": ROOT_URI}))["result"][
            "items"
        ] == []
        # Both channels are gone: subscribing yields the stateless-channel shape.
        assert (await client.request("subscribe", {"channel": uri}))["result"] == {}
        assert (await client.request("subscribe", {"channel": chat_uri}))["result"] == {}

    async def test_disposing_an_unknown_session_is_refused(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        response = await client.request("disposeSession", {"channel": "echo:/never-existed"})
        assert response["error"]["code"] == -32001

    async def test_actions_on_a_disposed_session_are_silently_ignored(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """Once the channel is gone the unknown-channel rule applies: no echo."""
        _, client = connected
        await _initialize(client)
        uri = "echo:/dispose-2"
        await client.request("createSession", {"channel": uri})
        await client.collect(seconds=0.3)
        await client.request("subscribe", {"channel": uri})
        await client.request("disposeSession", {"channel": uri})
        await client.collect(seconds=0.2)

        before = len(client.actions(uri))
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {"type": "session/titleChanged", "title": "ghost"},
            },
        )
        await client.collect(seconds=0.2)
        assert len(client.actions(uri)) == before


class TestPing:
    """`ping` is ungated by design.

    transport.md, Keep-Alive: "the server MUST respond regardless of whether the
    client has completed `initialize` or holds any subscriptions." It is how a
    client stops an idle-timeout proxy from closing the socket, so gating it on
    the handshake would break exactly the case it exists for.
    """

    async def test_ping_is_answered_before_initialize(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        response = await client_ping(connected[1])
        assert "error" not in response, response.get("error")
        assert response["result"] is None

    async def test_ping_is_answered_after_initialize(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        assert (await client_ping(client))["result"] is None


async def client_ping(client: FakeClient) -> dict[str, Any]:
    return await client.request("ping", {"channel": ROOT_URI})


class TestCustomizations:
    """A provider contributing customizations and tools to session state.

    The tree is two-level and easy to get wrong: the top-level `Customization`
    union is only `plugin` / `directory` / `mcpServer`, and agents, skills,
    prompts, rules and hooks are CHILDREN of a container. `serverTools` is a
    separate field, not a customization.
    """

    @staticmethod
    async def _state(client: FakeClient, uri: str) -> dict[str, Any]:
        await _initialize(client)
        await client.request("createSession", {"channel": uri})
        await client.collect(seconds=0.4)
        result = (await client.request("subscribe", {"channel": uri}))["result"]
        return dict(result["snapshot"]["state"])

    async def test_the_whole_tree_lands_in_session_state(self) -> None:
        host = Host(EchoProvider(customizations=True), LoopbackSingleUserPolicy())
        client_transport, server_transport = memory_pair()
        serve = asyncio.create_task(host.serve(server_transport))
        try:
            state = await self._state(FakeClient(client_transport), "echo:/cust-1")
            top = {c["type"]: c for c in state["customizations"]}
            assert set(top) == {"plugin", "directory", "mcpServer"}

            children = {c["type"] for c in top["plugin"]["children"]}
            assert children == {"agent", "skill", "prompt", "rule", "hook"}

            assert [t["name"] for t in state["serverTools"]] == [
                "ahs_echo_tool",
                "ahs_clock_tool",
            ]
        finally:
            serve.cancel()
            await host.aclose()

    async def test_customizations_are_published_before_ready(self) -> None:
        """So a client subscribing on `ready` sees them in its snapshot rather
        than racing for them."""
        host = Host(EchoProvider(customizations=True), LoopbackSingleUserPolicy())
        client_transport, server_transport = memory_pair()
        serve = asyncio.create_task(host.serve(server_transport))
        client = FakeClient(client_transport)
        try:
            await _initialize(client)
            uri = "echo:/cust-2"
            await client.request("createSession", {"channel": uri})
            await client.collect(seconds=0.4)

            # Asserted from the replay log rather than from what this client
            # happened to observe: bring-up is asynchronous, so a client that
            # subscribes after it finishes correctly sees the result in its
            # snapshot instead. The published ORDER is what matters here.
            replay = (
                await client.request(
                    "reconnect",
                    {
                        "channel": ROOT_URI,
                        "clientId": "test-client",
                        "lastSeenServerSeq": 0,
                        "subscriptions": [uri],
                    },
                )
            )["result"]
            order = [a["action"]["type"] for a in replay["actions"]]
            assert "session/customizationsChanged" in order, order
            assert order.index("session/customizationsChanged") < order.index("session/ready")
            assert order.index("session/serverToolsChanged") < order.index("session/ready")
        finally:
            serve.cancel()
            await host.aclose()

    async def test_they_are_off_by_default(self, connected: tuple[Host, FakeClient]) -> None:
        """The echo agent has no plugins; pretending otherwise would be a lie."""
        _, client = connected
        state = await self._state(client, "echo:/cust-3")
        assert not state.get("customizations")
        assert not state.get("serverTools")


class TestSessionCatalogue:
    """`listSessions` and `root/sessionSummaryChanged` are two views of one thing.

    In VS Code's Agents-app window the session list is the home screen, so a
    summary the host never mirrors is a stale home screen -- not a missing nicety.
    """

    @staticmethod
    async def _make(client: FakeClient, uri: str) -> None:
        await client.request("createSession", {"channel": uri, "provider": "echo"})
        await client.collect(seconds=0.3)

    async def test_a_title_change_is_mirrored_to_root(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        uri = "echo:/summary-1"
        await self._make(client, uri)
        client.notifications.clear()

        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {"type": "session/titleChanged", "title": "Renamed by a client"},
            },
        )
        await client.collect(seconds=0.3)

        changed = [
            n["params"]
            for n in client.notifications
            if n.get("method") == "root/sessionSummaryChanged"
        ]
        assert changed, "the root catalogue was never told the title moved"
        assert changed[-1]["session"] == uri
        assert changed[-1]["changes"]["title"] == "Renamed by a client"

    async def test_only_changed_fields_are_carried(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """ "Only fields present in `changes` have new values." Identity fields
        never change and are not carried at all."""
        _, client = connected
        await _initialize(client)
        uri = "echo:/summary-2"
        await self._make(client, uri)
        client.notifications.clear()

        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {"type": "session/titleChanged", "title": "Only the title"},
            },
        )
        await client.collect(seconds=0.3)
        changes = [
            n["params"]["changes"]
            for n in client.notifications
            if n.get("method") == "root/sessionSummaryChanged"
        ][-1]
        assert set(changes) == {"title"}
        for identity in ("resource", "provider", "createdAt"):
            assert identity not in changes

    async def test_an_unchanged_summary_emits_nothing(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """A client caches the session list; a no-op notification per streamed
        delta would be one notification per token, to every connected client."""
        _, client = connected
        await _initialize(client)
        uri = "echo:/summary-3"
        await self._make(client, uri)
        client.notifications.clear()

        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {"type": "session/titleChanged", "title": "New Session"},
            },
        )
        await client.collect(seconds=0.3)
        assert not [
            n for n in client.notifications if n.get("method") == "root/sessionSummaryChanged"
        ]

    async def test_list_sessions_pages_and_the_cursor_walks_the_catalogue(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        for index in range(5):
            await self._make(client, f"echo:/page-{index}")

        first = (await client.request("listSessions", {"channel": ROOT_URI, "limit": 2}))["result"]
        assert len(first["items"]) == 2
        assert "nextCursor" in first

        seen = [item["resource"] for item in first["items"]]
        cursor = first["nextCursor"]
        while cursor is not None:
            page = (
                await client.request(
                    "listSessions", {"channel": ROOT_URI, "limit": 2, "cursor": cursor}
                )
            )["result"]
            seen.extend(item["resource"] for item in page["items"])
            cursor = page.get("nextCursor")

        # Every session exactly once: a keyset cursor must not skip or repeat.
        assert sorted(seen) == sorted(f"echo:/page-{i}" for i in range(5))
        assert len(seen) == len(set(seen))

    async def test_the_last_page_carries_no_cursor(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """ "A missing `nextCursor` signals the end of the collection" -- so an
        eagerly-set one makes a client's paging loop never terminate."""
        _, client = connected
        await _initialize(client)
        await self._make(client, "echo:/only-one")
        result = (await client.request("listSessions", {"channel": ROOT_URI, "limit": 50}))[
            "result"
        ]
        assert len(result["items"]) == 1
        assert "nextCursor" not in result

    async def test_an_unrecognised_cursor_is_invalid_params(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        response = await client.request(
            "listSessions", {"channel": ROOT_URI, "cursor": "not-a-real-cursor"}
        )
        assert response["error"]["code"] == -32602


class TestFetchTurns:
    """This host keeps every turn in state, so there is never an older page --
    but the command is supported, and a client cannot tell "does not page" from
    "is broken" if we answer MethodNotFound."""

    async def test_dispatches_turns_loaded_before_responding(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """The MUST is unconditional: "the host MUST dispatch `chat/turnsLoaded`
        ... before responding"."""
        _, client = connected
        await _initialize(client)
        uri = "echo:/fetch-1"
        await client.request("createSession", {"channel": uri, "provider": "echo"})
        await client.collect(seconds=0.3)
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        chat_uri = state["chats"][0]["resource"]
        await client.request("subscribe", {"channel": chat_uri})
        client.notifications.clear()

        response = await client.request("fetchTurns", {"channel": chat_uri})
        assert response["result"] == {}
        loaded = [a for a in client.actions(chat_uri) if a["action"]["type"] == "chat/turnsLoaded"]
        assert loaded, "turnsLoaded must be dispatched even when nothing is loaded"

    async def test_any_cursor_is_unrecognised(self, connected: tuple[Host, FakeClient]) -> None:
        """ "The host MUST reject unrecognised cursors with `InvalidParams`" --
        and this host has never issued one."""
        _, client = connected
        await _initialize(client)
        uri = "echo:/fetch-2"
        await client.request("createSession", {"channel": uri, "provider": "echo"})
        await client.collect(seconds=0.3)
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        chat_uri = state["chats"][0]["resource"]

        response = await client.request("fetchTurns", {"channel": chat_uri, "cursor": "anything"})
        assert response["error"]["code"] == -32602

    async def test_a_non_chat_channel_is_refused(self, connected: tuple[Host, FakeClient]) -> None:
        """Asked of the sequencer's bound reducer, never of the URI's scheme."""
        _, client = connected
        await _initialize(client)
        response = await client.request("fetchTurns", {"channel": ROOT_URI})
        assert response["error"]["code"] == -32602


class TestWorkingDirectories:
    """The reducers apply these mutations verbatim, on purpose. Upstream:
    "the `immutablePrimary` guarantee therefore lives at the dispatch-validation
    / host-acceptance layer, not in the reducer"."""

    @staticmethod
    def _host(multiroot: dict[str, Any] | None = None) -> Host:
        capabilities = {"multipleWorkingDirectories": multiroot} if multiroot is not None else {}
        return Host(EchoProvider(capabilities=capabilities), LoopbackSingleUserPolicy())

    @staticmethod
    async def _connect(host: Host) -> tuple[FakeClient, asyncio.Task[None]]:
        client_transport, server_transport = memory_pair()
        serve = asyncio.create_task(host.serve(server_transport))
        client = FakeClient(client_transport)
        await _initialize(client)
        return client, serve

    async def test_create_session_seeds_the_declared_set(self) -> None:
        host = self._host({"immutablePrimary": False})
        client, serve = await self._connect(host)
        try:
            uri = "echo:/wd-1"
            await client.request(
                "createSession",
                {
                    "channel": uri,
                    "provider": "echo",
                    "workingDirectories": ["file:///a", "file:///b"],
                },
            )
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            assert state["workingDirectories"] == ["file:///a", "file:///b"]
        finally:
            serve.cancel()
            await host.aclose()

    async def test_without_the_capability_only_the_first_entry_survives(self) -> None:
        """ "Servers without that capability treat only the first entry as the
        session's working directory and ignore the rest.\""""
        host = self._host()
        client, serve = await self._connect(host)
        try:
            uri = "echo:/wd-2"
            await client.request(
                "createSession",
                {
                    "channel": uri,
                    "provider": "echo",
                    "workingDirectories": ["file:///a", "file:///b", "file:///c"],
                },
            )
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            assert state["workingDirectories"] == ["file:///a"]
        finally:
            serve.cancel()
            await host.aclose()

    async def test_without_the_capability_a_client_may_not_mutate_the_set(self) -> None:
        """ "When absent, clients MUST NOT mutate a session's or chat's
        working-directory set" -- a client MUST only the host can enforce."""
        host = self._host()
        client, serve = await self._connect(host)
        try:
            uri = "echo:/wd-3"
            await client.request(
                "createSession",
                {"channel": uri, "provider": "echo", "workingDirectories": ["file:///a"]},
            )
            await client.request("subscribe", {"channel": uri})
            await client.notify(
                "dispatchAction",
                {
                    "channel": uri,
                    "clientSeq": 1,
                    "action": {"type": "session/workingDirectorySet", "directory": "file:///evil"},
                },
            )
            await client.collect(seconds=0.3)
            echoes = [
                a
                for a in client.actions(uri)
                if a["action"]["type"] == "session/workingDirectorySet"
            ]
            assert echoes, "a rejected action MUST still be echoed so the client can revert"
            assert "rejectionReason" in echoes[-1]
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            assert state["workingDirectories"] == ["file:///a"]
        finally:
            serve.cancel()
            await host.aclose()

    async def test_the_immutable_primary_cannot_be_removed(self) -> None:
        host = self._host({"immutablePrimary": True})
        client, serve = await self._connect(host)
        try:
            uri = "echo:/wd-4"
            await client.request(
                "createSession",
                {
                    "channel": uri,
                    "provider": "echo",
                    "workingDirectories": ["file:///primary", "file:///peer"],
                },
            )
            await client.request("subscribe", {"channel": uri})
            for directory in ("file:///primary", "file:///peer"):
                await client.notify(
                    "dispatchAction",
                    {
                        "channel": uri,
                        "clientSeq": 1,
                        "action": {
                            "type": "session/workingDirectoryRemoved",
                            "directory": directory,
                        },
                    },
                )
            await client.collect(seconds=0.3)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            # The peer went; the primary is a fixed process root for the session.
            assert state["workingDirectories"] == ["file:///primary"]
        finally:
            serve.cancel()
            await host.aclose()

    async def test_a_policy_may_refuse_a_directory(self) -> None:
        class NoTmp(LoopbackSingleUserPolicy):
            def may_grant_working_directory(self, info: Any, session: str, directory: str) -> bool:
                return not directory.startswith("file:///tmp")

        host = Host(EchoProvider(capabilities={"multipleWorkingDirectories": {}}), NoTmp())
        client, serve = await self._connect(host)
        try:
            uri = "echo:/wd-5"
            await client.request(
                "createSession",
                {
                    "channel": uri,
                    "provider": "echo",
                    "workingDirectories": ["file:///work", "file:///tmp/secrets"],
                },
            )
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            assert state["workingDirectories"] == ["file:///work"]
        finally:
            serve.cancel()
            await host.aclose()


class TestAnnotationsChannel:
    """VS Code subscribes to `<session>/annotations` unconditionally and its
    client throws on a result with no `snapshot`
    (`remoteAgentHostProtocolClient.ts:863-869`). Before the channel was
    registered that fired on every connect."""

    async def test_a_session_exposes_an_annotations_channel(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        uri = "echo:/ann-1"
        await client.request("createSession", {"channel": uri, "provider": "echo"})
        await client.collect(seconds=0.3)

        response = await client.request("subscribe", {"channel": f"{uri}/annotations"})
        assert "snapshot" in response["result"], "a snapshot-less result throws in the client"
        assert response["result"]["snapshot"]["state"] == {"annotations": []}

    async def test_the_summary_carries_the_annotations_channel(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """`SessionSummary.annotations` exists so badge UI can render counts
        "without subscribing to the channel itself"."""
        _, client = connected
        await _initialize(client)
        uri = "echo:/ann-2"
        await client.request("createSession", {"channel": uri, "provider": "echo"})
        await client.collect(seconds=0.3)

        item = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]["items"][0]
        assert item["annotations"] == {
            "resource": f"{uri}/annotations",
            "annotationCount": 0,
            "entryCount": 0,
        }

    async def test_annotations_are_reduced_and_counted(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        """All five annotation actions are client-dispatchable, so registering
        the channel without its reducer would broadcast actions nothing applies."""
        _, client = connected
        await _initialize(client)
        uri = "echo:/ann-3"
        await client.request("createSession", {"channel": uri, "provider": "echo"})
        await client.collect(seconds=0.3)
        annotations_uri = f"{uri}/annotations"
        await client.request("subscribe", {"channel": annotations_uri})

        await client.notify(
            "dispatchAction",
            {
                "channel": annotations_uri,
                "clientSeq": 1,
                "action": {
                    "type": "annotations/set",
                    "annotation": {
                        "id": "a1",
                        "resource": "file:///x.py",
                        "entries": [{"id": "e1", "text": "look here"}],
                    },
                },
            },
        )
        await client.collect(seconds=0.3)

        state = (await client.request("subscribe", {"channel": annotations_uri}))["result"][
            "snapshot"
        ]["state"]
        assert [a["id"] for a in state["annotations"]] == ["a1"]

        item = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]["items"][0]
        assert item["annotations"]["annotationCount"] == 1
        assert item["annotations"]["entryCount"] == 1

    async def test_disposing_the_session_drops_the_annotations_channel(
        self, connected: tuple[Host, FakeClient]
    ) -> None:
        _, client = connected
        await _initialize(client)
        uri = "echo:/ann-4"
        await client.request("createSession", {"channel": uri, "provider": "echo"})
        await client.collect(seconds=0.3)
        await client.request("disposeSession", {"channel": uri})
        response = await client.request("subscribe", {"channel": f"{uri}/annotations"})
        assert response["result"] == {}, "the channel outlived its session"
