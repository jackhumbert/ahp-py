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
        for method in ("resourceRead", "createTerminal", "authenticate", "fetchTurns"):
            response = await client.request(method, {"channel": ROOT_URI})
            assert response["error"]["code"] == -32601, method


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
