"""The server->client half of a tool call: who may answer one, and with what.

Upstream's own reference client ships zero implementations of this direction,
so nothing else exercises it -- and all three defects below survived every
wire-level assertion in the suite, because each is a frame that looks perfectly
well-formed and that the *reducer* then refuses while the host acts on it
anyway. The provider and every client end up disagreeing about what happened to
a tool call, silently.

Three rules, each quoted from the vendored schema where it is stated:

- A park has a KIND. `SessionToolConfirmationRequest` says "Respond by
  dispatching `chat/toolCallConfirmed`"; `SessionToolClientExecutionRequest`
  says "Execute and report the result by dispatching `chat/toolCallComplete`"
  (`vendor/upstream/ts/session-state.ts`). Crossing them resolved a provider's
  request with a value that meant something else entirely.
- The owner executes. "The server SHOULD reject this action if the dispatching
  client does not match the contributor's `clientId`"
  (`ChatToolCallCompleteAction`).
- The owner leaving ends its calls. "When removing a client, the host SHOULD
  also cancel that client's in-flight tool calls ... by dispatching
  `chat/toolCallComplete` with `result.success = false`"
  (`SessionActiveClientRemovedAction`).
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

_TOOLS = [
    {"name": "usages", "title": "Find Usages", "inputSchema": {"type": "object"}},
]


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
    host: Host, client: FakeClient, uri: str, *, owner: str | None = None
) -> tuple[str, str]:
    params: dict[str, Any] = {"channel": uri, "provider": "echo"}
    if owner is not None:
        params["activeClient"] = {"clientId": owner, "displayName": "Owner", "tools": _TOOLS}
    await client.request("createSession", params)
    # The default chat, not a moment: the next line indexes `chats[0]` out of
    # the snapshot, so "the session has a chat" is the condition the fixed wait
    # this replaces was standing in for.
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("chats")), timeout=10.0
    )
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


async def _dispatch(client: FakeClient, channel: str, action: dict[str, Any]) -> None:
    await client.notify("dispatchAction", {"channel": channel, "clientSeq": 2, "action": action})


def _action(client: FakeClient, channel: str, kind: str) -> dict[str, Any] | None:
    for envelope in client.actions(channel):
        if envelope["action"]["type"] == kind:
            found: dict[str, Any] = envelope["action"]
            return found
    return None


def _echoes(client: FakeClient, channel: str, kind: str, origin: str) -> list[dict[str, Any]]:
    """This client's own echoes of *kind*, newest last.

    Filtered by origin because the host publishes actions of the same type
    itself, and an unfiltered search finds the host's frame instead.
    """
    return [
        envelope
        for envelope in client.actions(channel)
        if envelope["action"]["type"] == kind
        and envelope.get("origin", {}).get("clientId") == origin
    ]


def _rejection(client: FakeClient, channel: str, kind: str, origin: str) -> str | None:
    """The `rejectionReason` on this client's own echoed action, if any."""
    echoes = _echoes(client, channel, kind, origin)
    assert echoes, "a rejected action MUST still be echoed"
    reason: str | None = echoes[-1].get("rejectionReason")
    return reason


async def _refused(
    client: FakeClient, channel: str, kind: str, origin: str, *, timeout: float = 10.0
) -> None:
    """Wait for this client's own *kind* to come back carrying a rejection.

    The rejection IS the answer here: `_dispatch_action` publishes the echo and
    returns, so a refused action never reaches the park at all. Waiting for the
    echo rather than for a fixed moment leaves the caller's own assertions to
    say what went wrong -- an unrefused action times out and then fails on
    "the denial was not refused" rather than on a TimeoutError.
    """
    await client.collect_until(
        lambda: any(e.get("rejectionReason") for e in _echoes(client, channel, kind, origin)),
        timeout=timeout,
    )


async def _parked(host: Host, client: FakeClient, chat_uri: str, *, timeout: float = 10.0) -> None:
    """Wait for the call to be RUNNING on exactly one suspended request.

    Both halves, and the status is the load-bearing one. `run_client_tool` opens
    the park BEFORE it publishes `chat/toolCallStart`, and `chat/toolCallReady`
    -- the frame that actually moves the call to `running` -- comes after that
    again. So `len(host.pending) == 1` is true a moment before the state every
    caller of this goes on to assert on, and a wait on the park count alone
    returns early and fails on `'streaming' != 'running'` under any hiccup in
    that window. Measured: half a millisecond of delay on the ready publish is
    enough.
    """
    await client.collect_until(
        lambda: len(host.pending) == 1 and _tool_call(host, chat_uri).get("status") == "running",
        timeout=timeout,
    )


async def _confirming(
    host: Host, client: FakeClient, chat_uri: str, *, timeout: float = 10.0
) -> None:
    """Wait for the call to reach `pending-confirmation`.

    The STATUS, not the park count: these tests turn on which kind of answer a
    call is waiting for, and a park exists a moment before the reducer has moved
    the call into the state that says so.
    """
    await client.collect_until(
        lambda: _tool_call(host, chat_uri).get("status") == "pending-confirmation",
        timeout=timeout,
    )


async def _turn_over(client: FakeClient, chat_uri: str, *, timeout: float = 10.0) -> None:
    """Wait until *client* holds the turn's terminal frame.

    Client-side rather than host-side on purpose: frames arrive in order, so
    once `chat/turnComplete` is in hand every delta of that turn is too -- and
    the deltas are what the assertions after this read. A host-side condition
    would be satisfied while those frames were still in flight.
    """
    await client.collect_until(
        lambda: _action(client, chat_uri, "chat/turnComplete") is not None, timeout=timeout
    )


def _deltas(client: FakeClient, chat_uri: str) -> str:
    return "".join(
        envelope["action"].get("content", "")
        for envelope in client.actions(chat_uri)
        if envelope["action"]["type"] == "chat/delta"
    )


def _tool_call(host: Host, chat_uri: str) -> dict[str, Any]:
    """The REDUCED tool call -- what every client's mirror actually holds.

    Every defect in this file published a frame that read correctly on the wire
    and was then discarded by the reducer, so only the reduced state can tell
    the two apart.
    """
    state = host.sequencer.state_of(chat_uri)
    turn = state.get("activeTurn") or (state.get("turns") or [{}])[-1]
    for part in turn.get("responseParts", []):
        if part.get("kind") == "toolCall":
            call: dict[str, Any] = part["toolCall"]
            return call
    return {}


def _input_needed(host: Host, session_uri: str) -> list[dict[str, Any]]:
    state = host.sequencer.state_of(session_uri)
    entries = state.get("inputNeeded")
    return list(entries) if isinstance(entries, list) else []


class TestParkKind:
    """A `toolCallId` names a park, but not what that park is waiting FOR."""

    @pytest.fixture
    async def host(self) -> AsyncIterator[Host]:
        host = Host(EchoProvider(client_tools=True), LoopbackSingleUserPolicy())
        try:
            yield host
        finally:
            await host.aclose()

    async def _park(self, host: Host, uri: str) -> tuple[FakeClient, str, str]:
        client = await _attach(host, "owner")
        session_uri, chat_uri = await _session(host, client, uri, owner="owner")
        await _send(client, chat_uri)
        await _parked(host, client, chat_uri)
        assert len(host.pending) == 1, "the provider never asked the client to run anything"
        return client, session_uri, chat_uri

    async def test_a_refusal_is_not_an_empty_success(self, host: Host) -> None:
        """ "For client-provided tools, the owning client MUST dispatch this if
        it does not recognize the tool or cannot execute it"
        (`ChatToolCallDeniedAction`) -- but that action IS
        `chat/toolCallConfirmed` with `approved: false`, and the reducer applies
        it only from `pending-confirmation`. A client tool is `running`, so the
        refusal a running call can actually carry is a `chat/toolCallComplete`
        whose `result.success` is false.

        Either way the defect is the same and it is the reason this test
        exists: the refusal reached the provider as `ToolResult(value={})`,
        indistinguishable from a tool that ran and returned nothing, so the
        agent reported the editor's "no" as a result and carried on."""
        client, _, chat_uri = await self._park(host, "echo:/park-refuse")
        await _dispatch(
            client,
            chat_uri,
            {
                "type": "chat/toolCallComplete",
                "turnId": "t1",
                "toolCallId": "client-tool-1",
                "result": {
                    "success": False,
                    "content": [{"type": "text", "text": "no such tool here"}],
                    "pastTenseMessage": "Refused the call",
                },
            },
        )
        await _turn_over(client, chat_uri)

        deltas = _deltas(client, chat_uri)
        assert "refused" in deltas, deltas
        assert "said: {}" not in deltas, "a refusal was reported to the agent as an empty result"
        assert "said: None" not in deltas, "a refusal was reported to the agent as an empty result"
        assert len(host.pending) == 0, "the provider is still parked on an answered call"

    async def test_denying_a_running_call_points_at_the_right_action(self, host: Host) -> None:
        """The approval action cannot answer a running call in EITHER
        direction: the reducer takes `chat/toolCallConfirmed` only from
        `pending-confirmation`, so a denial aimed at a client tool would be a
        frame the host acted on and no reducer applied -- which is the whole
        defect class this file is about. Refused, with the mechanism named."""
        client, _, chat_uri = await self._park(host, "echo:/park-deny")
        await _dispatch(
            client,
            chat_uri,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": "t1",
                "toolCallId": "client-tool-1",
                "approved": False,
                "reason": "denied",
            },
        )
        await _refused(client, chat_uri, "chat/toolCallConfirmed", "owner")

        reason = _rejection(client, chat_uri, "chat/toolCallConfirmed", "owner")
        assert reason is not None, "the denial was not refused"
        assert "execute it and report the result" in reason, reason
        assert len(host.pending) == 1, "the park was resolved by an action no reducer applied"

    async def test_approving_a_call_the_client_was_asked_to_run_is_rejected(
        self, host: Host
    ) -> None:
        """The call is already `running` and its owner was asked to EXECUTE it.
        There is nothing to approve, the reducer no-ops the frame (fixture
        `096-toolcallconfirmed-wrong-status-is-no-op`), and the host used to
        wake the provider with an empty result anyway."""
        client, _, chat_uri = await self._park(host, "echo:/park-approve")
        await _dispatch(
            client,
            chat_uri,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": "t1",
                "toolCallId": "client-tool-1",
                "approved": True,
                "confirmed": "user-action",
            },
        )
        await _refused(client, chat_uri, "chat/toolCallConfirmed", "owner")

        reason = _rejection(client, chat_uri, "chat/toolCallConfirmed", "owner")
        assert reason is not None, "the action was not refused"
        assert "execut" in reason, reason
        assert len(host.pending) == 1, "the park was resolved by an action no reducer applied"
        assert _tool_call(host, chat_uri)["status"] == "running"

    async def test_a_result_cannot_answer_a_confirmation(self) -> None:
        """The mirror image: `chat/toolCallComplete` against a park that is
        waiting for approval. The reducer accepts a completion out of
        `pending-confirmation`, so state records the client's result -- while
        the host read that result as an approval, handed its `toolInput` to the
        provider, and ran the tool for real underneath it."""
        host = Host(EchoProvider(confirm_tools=True), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host, "owner")
            _, chat_uri = await _session(host, client, "echo:/park-complete")
            await _send(client, chat_uri)
            await _confirming(host, client, chat_uri)
            assert _tool_call(host, chat_uri)["status"] == "pending-confirmation"

            await _dispatch(
                client,
                chat_uri,
                {
                    "type": "chat/toolCallComplete",
                    "turnId": "t1",
                    "toolCallId": _tool_call(host, chat_uri)["toolCallId"],
                    "result": {"content": [], "success": True, "pastTenseMessage": "Did it"},
                },
            )
            await _refused(client, chat_uri, "chat/toolCallComplete", "owner")

            reason = _rejection(client, chat_uri, "chat/toolCallComplete", "owner")
            assert reason is not None, "the action was not refused"
            assert "confirmation" in reason, reason
            assert len(host.pending) == 1
            assert _tool_call(host, chat_uri)["status"] == "pending-confirmation"
        finally:
            await host.aclose()


class TestOwnership:
    """ "The identified client is responsible for executing the tool and
    dispatching `chat/toolCallComplete` with the result"
    (`ToolCallClientContributor.clientId`)."""

    @pytest.fixture
    async def host(self) -> AsyncIterator[Host]:
        host = Host(EchoProvider(client_tools=True), LoopbackSingleUserPolicy())
        try:
            yield host
        finally:
            await host.aclose()

    async def test_a_foreign_client_cannot_complete_the_call(self, host: Host) -> None:
        """Another window's payload was attributed to the owner: the provider
        consumed a result from a client that was never asked, and the transcript
        recorded it as the owner's work."""
        owner = await _attach(host, "owner")
        _, chat_uri = await _session(host, owner, "echo:/own-1", owner="owner")
        intruder = await _attach(host, "intruder")
        await intruder.request("subscribe", {"channel": chat_uri})
        await _send(owner, chat_uri)
        await _parked(host, owner, chat_uri)
        assert len(host.pending) == 1

        await _dispatch(
            intruder,
            chat_uri,
            {
                "type": "chat/toolCallComplete",
                "turnId": "t1",
                "toolCallId": "client-tool-1",
                "result": {"content": [], "success": True, "pastTenseMessage": "Not mine to run"},
            },
        )
        await _refused(intruder, chat_uri, "chat/toolCallComplete", "intruder")

        reason = _rejection(intruder, chat_uri, "chat/toolCallComplete", "intruder")
        assert reason is not None, "the intruder's completion was not refused"
        assert "owning client" in reason, reason
        assert len(host.pending) == 1, "a client that does not own the call answered it"
        assert _tool_call(host, chat_uri)["status"] == "running"

    async def test_the_owner_still_completes_it(self, host: Host) -> None:
        """The ownership check must not break the flow it protects."""
        owner = await _attach(host, "owner")
        _, chat_uri = await _session(host, owner, "echo:/own-2", owner="owner")
        await _send(owner, chat_uri)
        await _parked(host, owner, chat_uri)

        await _dispatch(
            owner,
            chat_uri,
            {
                "type": "chat/toolCallComplete",
                "turnId": "t1",
                "toolCallId": "client-tool-1",
                "result": {
                    "content": [{"type": "text", "text": "ran it locally"}],
                    "success": True,
                    "pastTenseMessage": "Ran it",
                },
            },
        )
        await _turn_over(owner, chat_uri)
        assert "ran it locally" in _deltas(owner, chat_uri)
        assert len(host.pending) == 0

    async def test_a_confirmation_for_a_server_tool_is_not_ownership_gated(self) -> None:
        """Deliberately NOT symmetrical. The protocol's validation table
        conditions `chat/toolCallConfirmed` on the call's STATUS and never on
        identity, so any subscriber may approve a server-side tool -- that is
        how a second window approves what the first one started."""
        host = Host(EchoProvider(confirm_tools=True), LoopbackSingleUserPolicy())
        try:
            starter = await _attach(host, "starter")
            _, chat_uri = await _session(host, starter, "echo:/own-3")
            approver = await _attach(host, "approver")
            await approver.request("subscribe", {"channel": chat_uri})
            await _send(starter, chat_uri)
            await _confirming(host, starter, chat_uri)

            await _dispatch(
                approver,
                chat_uri,
                {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": _tool_call(host, chat_uri)["toolCallId"],
                    "approved": True,
                    "confirmed": "user-action",
                },
            )
            # Both halves: the echo, which `_rejection` needs before it can
            # report the absence of a reason, and the resolved park, which is
            # what an accepted approval actually does. A refusal would leave the
            # park open, so this waits out its timeout and then fails on the
            # assertion rather than passing early on a half-applied frame.
            await approver.collect_until(
                lambda: (
                    bool(_echoes(approver, chat_uri, "chat/toolCallConfirmed", "approver"))
                    and len(host.pending) == 0
                ),
                timeout=10.0,
            )
            assert _rejection(approver, chat_uri, "chat/toolCallConfirmed", "approver") is None
            assert len(host.pending) == 0
        finally:
            await host.aclose()


class TestOwnerLeaves:
    """ "When removing a client, the host SHOULD also cancel that client's
    in-flight tool calls ... by dispatching `chat/toolCallComplete` with
    `result.success = false`. (There is no per-tool-call server cancel; a failed
    completion is the cancellation mechanism.)"

    Without it the session keeps advertising a `toolClientExecution` for a
    client that is gone, stays pinned at `InputNeeded`, and the parked request
    outlives `disposeSession`.
    """

    @pytest.fixture
    async def host(self) -> AsyncIterator[Host]:
        host = Host(EchoProvider(client_tools=True), LoopbackSingleUserPolicy())
        try:
            yield host
        finally:
            await host.aclose()

    async def _park(self, host: Host, uri: str) -> tuple[FakeClient, FakeClient, str, str]:
        owner = await _attach(host, "owner")
        session_uri, chat_uri = await _session(host, owner, uri, owner="owner")
        watcher = await _attach(host, "watcher")
        await watcher.request("subscribe", {"channel": session_uri})
        await watcher.request("subscribe", {"channel": chat_uri})
        await _send(owner, chat_uri)
        # The advertisement as well as the park: these tests are about what
        # happens to `inputNeeded` when the owner goes, so it has to be there
        # before the owner goes.
        await owner.collect_until(
            lambda: len(host.pending) == 1 and bool(_input_needed(host, session_uri)),
            timeout=10.0,
        )
        assert len(host.pending) == 1
        assert _input_needed(host, session_uri)[0]["kind"] == "toolClientExecution"
        return owner, watcher, session_uri, chat_uri

    async def test_a_disconnecting_owner_fails_its_in_flight_calls(self, host: Host) -> None:
        owner, watcher, session_uri, chat_uri = await self._park(host, "echo:/leave-1")

        await owner.transport.close()
        # Every end state this test asserts, including the watcher's own copy of
        # the terminal frame -- `chat/turnComplete` is the last of them, so a
        # condition that stopped short of it could still be read too early.
        await watcher.collect_until(
            lambda: (
                _tool_call(host, chat_uri).get("status") == "completed"
                and _input_needed(host, session_uri) == []
                and len(host.pending) == 0
                and _action(watcher, chat_uri, "chat/turnComplete") is not None
            ),
            timeout=10.0,
        )

        call = _tool_call(host, chat_uri)
        assert call["status"] == "completed", "the call is still waiting on a client that is gone"
        assert call["success"] is False
        assert _input_needed(host, session_uri) == [], "the session still advertises the work"
        assert len(host.pending) == 0, "the parked request outlived its owner"
        # And the turn actually ended, rather than sitting open forever.
        assert _action(watcher, chat_uri, "chat/turnComplete") is not None

    async def test_a_client_that_removes_itself_fails_its_in_flight_calls(self, host: Host) -> None:
        """The other half of the same SHOULD: a client that is still connected
        but has left the session ("when it unsubscribes from the session
        channel") is just as unable to answer."""
        owner, watcher, session_uri, chat_uri = await self._park(host, "echo:/leave-2")

        await _dispatch(
            owner, session_uri, {"type": "session/activeClientRemoved", "clientId": "owner"}
        )
        await watcher.collect_until(
            lambda: (
                _tool_call(host, chat_uri).get("status") == "completed"
                and _input_needed(host, session_uri) == []
                and len(host.pending) == 0
            ),
            timeout=10.0,
        )

        call = _tool_call(host, chat_uri)
        assert call["status"] == "completed"
        assert call["success"] is False
        assert _input_needed(host, session_uri) == []
        assert len(host.pending) == 0

    async def test_another_clients_departure_leaves_the_call_alone(self, host: Host) -> None:
        """Scoped to the departing client's OWN calls. A second window closing
        must not cancel work the first one is doing."""
        owner, watcher, session_uri, chat_uri = await self._park(host, "echo:/leave-3")

        await watcher.transport.close()
        # A FIXED wait, kept deliberately. Every assertion below is negative --
        # nothing changed -- and the state they read already holds, so a
        # condition wait would return instantly and prove only that the host had
        # not yet reacted to the disconnect. The elapsed time IS the test.
        await owner.collect(seconds=0.4)

        assert _tool_call(host, chat_uri)["status"] == "running"
        assert len(host.pending) == 1
        assert _input_needed(host, session_uri) != []
