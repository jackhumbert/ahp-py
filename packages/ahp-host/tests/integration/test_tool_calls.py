"""Tool-call confirmation, client-executed tools, and the active-client list.

Both flows sit on ADR 0005's suspending primitive, and both are things the host
previously could not express at all: it discarded `createSession.activeClient`
entirely, and had no way for a provider to wait for anyone.

The client-tool flow is the interesting one. The host emits a tool call marked
`contributor: {kind: 'client'}`, the client executes it in its own process, and
the client reports the result. That gives an agent the editor's own tools with
**no filesystem API on the host at all**.
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
from agent_host_server.provider.base import TurnSink, UserMessage

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_TOOLS = [{"name": "echo", "description": "Echo text back", "inputSchema": {"type": "object"}}]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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


async def _session(
    client: FakeClient, uri: str, *, client_id: str | None = None
) -> tuple[str, str]:
    params: dict[str, Any] = {"channel": uri, "provider": "echo"}
    if client_id is not None:
        params["activeClient"] = {
            "clientId": client_id,
            "displayName": "Test Client",
            "tools": _TOOLS,
        }
    await client.request("createSession", params)
    await client.collect(seconds=0.3)
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat_uri: str = state["chats"][0]["resource"]
    await client.request("subscribe", {"channel": chat_uri})
    return uri, chat_uri


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


def _action(client: FakeClient, channel: str, kind: str) -> dict[str, Any] | None:
    for envelope in client.actions(channel):
        if envelope["action"]["type"] == kind:
            action: dict[str, Any] = envelope["action"]
            return action
    return None


def _deltas(client: FakeClient, chat_uri: str) -> str:
    return "".join(
        envelope["action"].get("content", "")
        for envelope in client.actions(chat_uri)
        if envelope["action"]["type"] == "chat/delta"
    )


class TestActiveClients:
    async def test_create_session_seeds_the_clients_published_tools(self) -> None:
        """VS Code ships its entire tool set here, with input schemas. Dropping
        it throws away the one tool surface that needs no filesystem API."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host, "vscode")
            uri, _ = await _session(client, "echo:/tools-1", client_id="vscode")
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            assert [c["clientId"] for c in state["activeClients"]] == ["vscode"]
            assert state["activeClients"][0]["tools"] == _TOOLS
        finally:
            await host.aclose()

    async def test_a_disconnecting_client_is_removed(self) -> None:
        """ "The server SHOULD automatically dispatch that removal when an active
        client disconnects." Otherwise the session advertises tools nobody can
        execute."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            owner = await _attach(host, "owner")
            watcher = await _attach(host, "watcher")
            uri, _ = await _session(owner, "echo:/tools-2", client_id="owner")
            await watcher.request("subscribe", {"channel": uri})

            await owner.transport.close()
            await watcher.collect(seconds=0.4)

            state = (await watcher.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            assert state["activeClients"] == []
        finally:
            await host.aclose()

    async def test_an_entry_without_a_client_id_is_dropped(self) -> None:
        """Unaddressable: nothing could update it, remove it, or be told to run
        its tools."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host, "vscode")
            await client.request(
                "createSession",
                {
                    "channel": "echo:/tools-3",
                    "provider": "echo",
                    "activeClient": {"displayName": "Nameless", "tools": []},
                },
            )
            await client.collect(seconds=0.3)
            state = (await client.request("subscribe", {"channel": "echo:/tools-3"}))["result"][
                "snapshot"
            ]["state"]
            assert state["activeClients"] == []
        finally:
            await host.aclose()


class TestToolConfirmation:
    @pytest.fixture
    async def host(self) -> AsyncIterator[Host]:
        host = Host(EchoProvider(confirm_tools=True), LoopbackSingleUserPolicy())
        try:
            yield host
        finally:
            await host.aclose()

    async def test_the_agent_waits_for_approval(self, host: Host) -> None:
        client = await _attach(host, "solo")
        _, chat_uri = await _session(client, "echo:/confirm-1")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)

        ready = _action(client, chat_uri, "chat/toolCallReady")
        assert ready is not None, "the agent never asked"
        assert ready["confirmationTitle"] == "Run echo tool"
        assert len(host.pending) == 1, "the agent did not suspend"

    async def test_approving_runs_the_tool(self, host: Host) -> None:
        client = await _attach(host, "solo")
        _, chat_uri = await _session(client, "echo:/confirm-2")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)
        ready = _action(client, chat_uri, "chat/toolCallReady")
        assert ready is not None

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": ready["toolCallId"],
                    "approved": True,
                    "confirmed": "user-action",
                },
            },
        )
        await client.collect(seconds=0.5)
        assert "You said: hello" in _deltas(client, chat_uri)

    async def test_an_edited_input_is_what_actually_runs(self, host: Host) -> None:
        """`editable` lets a client rewrite the parameters. Running the proposed
        input instead would execute something nobody agreed to."""
        client = await _attach(host, "solo")
        _, chat_uri = await _session(client, "echo:/confirm-3")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)
        ready = _action(client, chat_uri, "chat/toolCallReady")
        assert ready is not None

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": ready["toolCallId"],
                    "approved": True,
                    "confirmed": "user-action",
                    "editedToolInput": {"text": "edited by the client"},
                },
            },
        )
        await client.collect(seconds=0.5)
        deltas = _deltas(client, chat_uri)
        assert "edited by the client" in deltas
        assert "hello" not in deltas, "the proposed input ran instead of the approved one"

    async def test_denying_does_not_run_the_tool(self, host: Host) -> None:
        client = await _attach(host, "solo")
        _, chat_uri = await _session(client, "echo:/confirm-4")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)
        ready = _action(client, chat_uri, "chat/toolCallReady")
        assert ready is not None

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": ready["toolCallId"],
                    "approved": False,
                    "reason": "denied",
                },
            },
        )
        await client.collect(seconds=0.5)
        assert "(denied)" in _deltas(client, chat_uri)

    async def test_a_forged_tool_call_id_is_rejected(self, host: Host) -> None:
        client = await _attach(host, "solo")
        _, chat_uri = await _session(client, "echo:/confirm-5")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": "not-a-real-call",
                    "approved": True,
                    "confirmed": "user-action",
                },
            },
        )
        await client.collect(seconds=0.3)
        echoes = [
            envelope
            for envelope in client.actions(chat_uri)
            if envelope["action"]["type"] == "chat/toolCallConfirmed"
        ]
        assert echoes, "a rejected action MUST still be echoed"
        assert "rejectionReason" in echoes[-1]
        assert len(host.pending) == 1, "the real request was resolved by a forged id"


class TestClientTools:
    @pytest.fixture
    async def host(self) -> AsyncIterator[Host]:
        host = Host(EchoProvider(client_tools=True), LoopbackSingleUserPolicy())
        try:
            yield host
        finally:
            await host.aclose()

    async def test_the_host_asks_the_client_to_execute(self, host: Host) -> None:
        client = await _attach(host, "vscode")
        _, chat_uri = await _session(client, "echo:/client-1", client_id="vscode")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)

        start = _action(client, chat_uri, "chat/toolCallStart")
        assert start is not None
        # This is what makes the client responsible for running it.
        assert start["contributor"] == {"kind": "client", "clientId": "vscode"}

    async def test_the_clients_result_reaches_the_agent(self, host: Host) -> None:
        client = await _attach(host, "vscode")
        _, chat_uri = await _session(client, "echo:/client-2", client_id="vscode")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)
        start = _action(client, chat_uri, "chat/toolCallStart")
        assert start is not None

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/toolCallComplete",
                    "turnId": "t1",
                    "toolCallId": start["toolCallId"],
                    "result": {"content": [{"kind": "text", "text": "ran it locally"}]},
                },
            },
        )
        await client.collect(seconds=0.5)
        assert "ran it locally" in _deltas(client, chat_uri)

    async def test_it_is_surfaced_on_the_session_so_a_client_need_not_subscribe(
        self, host: Host
    ) -> None:
        """ "Surfaced so a client that provides the tool can pick up the work
        without subscribing to the owning chat." """
        client = await _attach(host, "vscode")
        session_uri, chat_uri = await _session(client, "echo:/client-3", client_id="vscode")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)

        state = (await client.request("subscribe", {"channel": session_uri}))["result"]["snapshot"][
            "state"
        ]
        entries = state.get("inputNeeded") or []
        assert entries, "the session never surfaced the work"
        assert entries[0]["kind"] == "toolClientExecution"
        assert entries[0]["clientId"] == "vscode"
        assert entries[0]["chat"] == chat_uri
        # And the session status carries InputNeeded, which is what a session
        # list renders.
        assert state["status"] & 24 == 24

    async def test_the_session_entry_is_retracted_once_answered(self, host: Host) -> None:
        client = await _attach(host, "vscode")
        session_uri, chat_uri = await _session(client, "echo:/client-4", client_id="vscode")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)
        start = _action(client, chat_uri, "chat/toolCallStart")
        assert start is not None

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/toolCallComplete",
                    "turnId": "t1",
                    "toolCallId": start["toolCallId"],
                    "result": {"content": []},
                },
            },
        )
        await client.collect(seconds=0.5)
        state = (await client.request("subscribe", {"channel": session_uri}))["result"]["snapshot"][
            "state"
        ]
        assert state.get("inputNeeded", []) == [], "the session still shows answered work"

    async def test_cancelling_the_turn_retracts_the_session_entry(self, host: Host) -> None:
        """A session left advertising work nobody can do stays InputNeeded until
        it is disposed."""
        client = await _attach(host, "vscode")
        session_uri, chat_uri = await _session(client, "echo:/client-5", client_id="vscode")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)

        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {"type": "chat/turnCancelled", "turnId": "t1"},
            },
        )
        await client.collect(seconds=0.5)
        state = (await client.request("subscribe", {"channel": session_uri}))["result"]["snapshot"][
            "state"
        ]
        assert state.get("inputNeeded", []) == []
        assert len(host.pending) == 0

    async def test_a_tool_for_an_absent_client_fails_rather_than_hangs(self, host: Host) -> None:
        """Nobody can answer a request addressed to a client that is not here,
        and an error the adapter can handle beats a hang it cannot see."""
        client = await _attach(host, "vscode")
        # No `activeClient` on createSession, so the provider has nobody to ask.
        _, chat_uri = await _session(client, "echo:/client-6")
        await _send(client, chat_uri)
        await client.collect(seconds=0.5)
        assert "(no active client to run a tool)" in _deltas(client, chat_uri)
        assert len(host.pending) == 0


def _tool_call(host: Host, chat_uri: str) -> dict[str, Any]:
    """The reduced tool call -- i.e. what every client's mirror holds for it.

    Asserting on the published frames alone is what let three of the defects in
    this file ship: each was a frame that looked right on the wire and that the
    reducer then discarded, so the host and every client disagreed about a tool
    call while every wire-level assertion stayed green.
    """
    state = host.sequencer.state_of(chat_uri)
    turn = state.get("activeTurn") or (state.get("turns") or [{}])[-1]
    for part in turn.get("responseParts", []):
        if part.get("kind") == "toolCall":
            call: dict[str, Any] = part["toolCall"]
            return call
    return {}


class _Plain(EchoProvider):
    """The simplest provider there is: announce a call, then finish it.

    No confirmation, which is the mode every server-side tool runs in -- and the
    mode in which the sink's own streaming methods were dropped on the floor.
    """

    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        super().__init__()
        self.gate = gate

    async def create_session(self, context: Any) -> Any:
        session = await super().create_session(context)
        gate = self.gate

        async def run(message: UserMessage, sink: TurnSink) -> None:
            await sink.tool_call_started(
                "call-1", "streamer", {"path": "/tmp/x"}, display_name="Streamer"
            )
            await sink.tool_call_output("call-1", [{"type": "text", "text": "PARTIAL"}])
            if gate is not None:
                await gate.wait()
            await sink.tool_call_completed(
                "call-1",
                {"content": [{"type": "text", "text": "FINAL"}]},
                past_tense_message="Streamed it",
            )

        session.send_user_message = run  # type: ignore[method-assign]
        return session


class TestPlainToolCalls:
    """A tool call that needs no approval -- including the host's own `!command`."""

    async def test_the_auto_confirming_ready_restates_the_calls_own_details(self) -> None:
        """`invocationMessage` is REQUIRED on `chat/toolCallReady`, and for a
        call leaving `streaming` the reducer takes both it and `toolInput` FROM
        THAT ACTION. Omitting them stored nulls over everything the provider had
        said about the call at the very moment it started running."""
        host = Host(_Plain(), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host, "solo")
            _, chat_uri = await _session(client, "echo:/plain-1")
            await _send(client, chat_uri)
            await client.collect(seconds=0.5)

            ready = _action(client, chat_uri, "chat/toolCallReady")
            assert ready is not None
            assert ready["invocationMessage"], "REQUIRED by ChatToolCallReadyAction"
            assert ready["toolInput"] == '{"path": "/tmp/x"}'

            call = _tool_call(host, chat_uri)
            assert call["invocationMessage"] == "Running Streamer"
            assert call["toolInput"] == '{"path": "/tmp/x"}'
        finally:
            await host.aclose()

    async def test_the_start_action_carries_no_tool_input(self) -> None:
        """`ChatToolCallStartAction` declares `toolName`, `displayName`,
        `intention` and `contributor` -- there is no `toolInput` on it, so one
        published there is dropped by the reducer and no non-confirming tool
        ever showed its input."""
        host = Host(_Plain(), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host, "solo")
            _, chat_uri = await _session(client, "echo:/plain-2")
            await _send(client, chat_uri)
            await client.collect(seconds=0.5)

            start = _action(client, chat_uri, "chat/toolCallStart")
            assert start is not None
            assert "toolInput" not in start
            # Moved, not lost: the input still reaches the client's state.
            assert _tool_call(host, chat_uri)["toolInput"] == '{"path": "/tmp/x"}'
        finally:
            await host.aclose()

    async def test_partial_output_lands_while_the_tool_is_still_running(self) -> None:
        """`chat/toolCallContentChanged` reaches a `running` call and no other,
        and the plain path only left `streaming` at completion -- so every
        partial this method exists to publish was discarded, and live output
        first appeared as the finished result."""
        gate = asyncio.Event()
        host = Host(_Plain(gate=gate), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host, "solo")
            _, chat_uri = await _session(client, "echo:/plain-3")
            await _send(client, chat_uri)
            await client.collect(seconds=0.4)

            call = _tool_call(host, chat_uri)
            assert call["status"] == "running"
            assert call["content"] == [{"type": "text", "text": "PARTIAL"}]
        finally:
            gate.set()
            await host.aclose()


class TestProgressAfterConfirmation:
    """The shipped `EchoProvider(confirm_tools=True)` streams its progress
    through `tool_call_delta` after the call was approved, which is precisely
    where `chat/toolCallDelta` is a no-op: the reducer's updater returns a
    running call untouched. The demo's own progress line never moved."""

    @pytest.fixture
    async def host(self) -> AsyncIterator[Host]:
        host = Host(EchoProvider(confirm_tools=True), LoopbackSingleUserPolicy())
        try:
            yield host
        finally:
            await host.aclose()

    async def _approve(self, client: FakeClient, chat_uri: str) -> None:
        ready = _action(client, chat_uri, "chat/toolCallReady")
        assert ready is not None
        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 2,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": ready["toolCallId"],
                    "approved": True,
                    "confirmed": "user-action",
                },
            },
        )

    async def test_progress_reaches_the_mirror(self, host: Host) -> None:
        client = await _attach(host, "solo")
        _, chat_uri = await _session(client, "echo:/progress-1")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)
        await self._approve(client, chat_uri)
        await client.collect(seconds=0.6)

        call = _tool_call(host, chat_uri)
        assert call["invocationMessage"] == "Echoing word 1"
        # The confirmation is carried forward rather than overwritten: a
        # `not-needed` here would record that nobody was ever asked.
        assert call["confirmed"] == "user-action"

    async def test_progress_does_not_wipe_the_streamed_output(self, host: Host) -> None:
        """A second `chat/toolCallReady` rebuilds the call from its base fields,
        and `content` is not one of them."""
        client = await _attach(host, "solo")
        _, chat_uri = await _session(client, "echo:/progress-2")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)
        await self._approve(client, chat_uri)
        await client.collect(seconds=0.6)

        assert _tool_call(host, chat_uri)["content"] == [{"type": "text", "text": "hello"}]


class TestMalformedToolCallActions:
    """A tool-call action missing a REQUIRED field is applied by no reducer --
    but the host resolves the parked request by `toolCallId` alone, so the tool
    ran anyway and every client was left with a call state said was never
    answered. Rejected at the boundary, with the echo that lets a client revert
    its optimistic prediction."""

    @pytest.fixture
    async def host(self) -> AsyncIterator[Host]:
        host = Host(EchoProvider(confirm_tools=True), LoopbackSingleUserPolicy())
        try:
            yield host
        finally:
            await host.aclose()

    async def _dispatch(
        self, host: Host, client: FakeClient, chat_uri: str, action: dict[str, Any]
    ) -> str | None:
        await client.notify(
            "dispatchAction", {"channel": chat_uri, "clientSeq": 2, "action": action}
        )
        await client.collect(seconds=0.4)
        echoes = [
            envelope
            for envelope in client.actions(chat_uri)
            if envelope["action"]["type"] == action["type"]
            and envelope["action"] is not action
            and envelope.get("origin", {}).get("clientId") == "solo"
        ]
        assert echoes, "a rejected action MUST still be echoed"
        reason: str | None = echoes[-1].get("rejectionReason")
        return reason

    @pytest.mark.parametrize(
        ("missing", "action"),
        [
            (
                "turnId",
                {"type": "chat/toolCallConfirmed", "approved": True, "confirmed": "user-action"},
            ),
            ("confirmed", {"type": "chat/toolCallConfirmed", "turnId": "t1", "approved": True}),
            ("reason", {"type": "chat/toolCallConfirmed", "turnId": "t1", "approved": False}),
            ("approved", {"type": "chat/toolCallConfirmed", "turnId": "t1"}),
            ("result", {"type": "chat/toolCallComplete", "turnId": "t1"}),
        ],
    )
    async def test_a_missing_required_field_is_rejected(
        self, host: Host, missing: str, action: dict[str, Any]
    ) -> None:
        client = await _attach(host, "solo")
        _, chat_uri = await _session(client, f"echo:/malformed-{missing}")
        await _send(client, chat_uri)
        await client.collect(seconds=0.4)
        ready = _action(client, chat_uri, "chat/toolCallReady")
        assert ready is not None

        reason = await self._dispatch(
            host, client, chat_uri, {**action, "toolCallId": ready["toolCallId"]}
        )
        assert reason is not None
        assert missing in reason

        # And the provider is still parked: the tool did NOT run behind state's
        # back, which is the whole damage this check exists to prevent.
        assert len(host.pending) == 1
        assert _tool_call(host, chat_uri)["status"] == "pending-confirmation"
