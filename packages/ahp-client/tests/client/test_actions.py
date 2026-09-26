"""The turn-scoped chat actions, held against the reducer that consumes them.

Every claim here is checked by applying the action this client puts on the wire
to the **shared** chat reducer -- the one gated on upstream's own fixture corpus
-- rather than to an assertion about key names. That is where the whole cluster
lived: five actions the host accepted, assigned a `serverSeq` and fanned out to
every subscriber, each of which then reduced to nothing because `turnId` was
missing. Nothing on either peer said so.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agent_host_protocol.reducers import chat_reducer

from agent_host_client.api import ToolCallReady, connect
from agent_host_client.api.events import ToolCallResultReview
from agent_host_client.client import actions
from agent_host_client.serve import ClientToolHost, InputResponder
from agent_host_client.testing import FakeHost, echo_host

CHAT = "ahp-chat://c/s"
TURN = "turn-1"

SESSION_STATE = {"lifecycle": "ready", "defaultChat": CHAT, "interactivity": "full"}


def _session_host() -> FakeHost:
    host = echo_host()

    def subscribe(params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        if channel.startswith("ahp-chat:"):
            body: Any = {"turns": [], "activeTurn": None}
        elif channel == "ahp-root://":
            body = host.root_state
        else:
            body = SESSION_STATE
        return {"snapshot": {"resource": channel, "state": body, "fromSeq": host._server_seq}}

    host.on("subscribe", subscribe)
    host.on("createSession", lambda _p: {})
    host.on("disposeSession", lambda _p: {})
    return host


def _dispatched(host: FakeHost) -> list[dict[str, Any]]:
    return [m["params"]["action"] for m in host.received if m.get("method") == "dispatchAction"]


def _chat_with_tool_call(status: str, **extra: Any) -> dict[str, Any]:
    """A chat mid-turn with one tool call in *status*.

    The tool call lives at `activeTurn.responseParts[].toolCall`, keyed by
    `toolCallId`, and the reducer will not look at it at all unless the action's
    `turnId` matches `activeTurn.id`.
    """
    return {
        "resource": CHAT,
        "title": "t",
        "status": 8,
        "turns": [],
        "activeTurn": {
            "id": TURN,
            "startedAt": "2026-01-01T00:00:00.000Z",
            "message": {"text": "go", "origin": {"kind": "user"}},
            "responseParts": [
                {
                    "kind": "toolCall",
                    "toolCall": {
                        "toolCallId": "tc1",
                        "toolName": "echo_tool",
                        "status": status,
                        "toolInput": '{"text": "orig"}',
                        **extra,
                    },
                }
            ],
        },
    }


def _tool_call_after(state: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    reduced = chat_reducer(state, action)
    active = reduced.get("activeTurn") or {}
    parts = active.get("responseParts") or []
    return dict(parts[0]["toolCall"]) if parts else {}


# ── starting a turn ──────────────────────────────────────────────────────────


async def test_a_client_started_turn_records_when_it_started() -> None:
    """`startedAt` is required and the reducer copies it verbatim, so omitting
    it stores `null` on every peer for the life of the transcript."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        started = asyncio.get_running_loop().create_task(
            chat.prompt("hello", turn_id=TURN).__anext__()
        )
        await asyncio.sleep(0.05)
        started.cancel()
    await host.stop()

    action = _dispatched(host)[-1]
    assert action["type"] == "chat/turnStarted"
    assert action["startedAt"].endswith("Z")
    # `Message` requires an origin, and `user` is the only kind a client may
    # produce -- a turn stored without it cannot be attributed on replay.
    assert action["message"]["origin"] == {"kind": "user"}

    idle = {"resource": CHAT, "title": "t", "status": 1, "turns": [], "activeTurn": None}
    active = chat_reducer(idle, action)["activeTurn"]
    assert active["startedAt"] == action["startedAt"]


# ── cancelling one ───────────────────────────────────────────────────────────


async def test_cancel_names_the_turn_so_the_reducer_can_settle_it() -> None:
    """Without `turnId` the cancel is a no-op on both peers while the host
    still tears the provider down: `activeTurn` stays open forever and every
    later `chat/turnStarted` is refused as "a turn is already active"."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        # A turn somebody else started -- the case `Chat.cancel()` cannot read
        # off a `TurnStream`, and the one that has to work anyway.
        await host.push(
            chat.uri,
            {
                "type": "chat/turnStarted",
                "turnId": TURN,
                "startedAt": "2026-01-01T00:00:00.000Z",
                "message": {"text": "theirs", "origin": {"kind": "user"}},
            },
        )
        await asyncio.sleep(0.05)
        await chat.cancel()
        await asyncio.sleep(0.02)
    await host.stop()

    action = _dispatched(host)[-1]
    assert action["type"] == "chat/turnCancelled"
    assert action["turnId"] == TURN
    assert action["duration"] == 0

    settled = chat_reducer(_chat_with_tool_call("running"), action)
    assert settled.get("activeTurn") is None
    assert settled["turns"][-1]["state"] == "cancelled"


async def test_cancelling_our_own_turn_reports_our_own_elapsed_time() -> None:
    """`duration` is producer-measured; a client "MUST NOT derive this by
    subtracting timestamps", so the only honest non-zero comes from the clock
    that dispatched the `chat/turnStarted`."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        started = asyncio.get_running_loop().create_task(
            chat.prompt("hello", turn_id=TURN).__anext__()
        )
        await asyncio.sleep(0.05)
        await chat.cancel()
        await asyncio.sleep(0.02)
        started.cancel()
    await host.stop()

    action = _dispatched(host)[-1]
    assert action["turnId"] == TURN
    assert action["duration"] > 0


async def test_cancelling_an_idle_chat_sends_nothing() -> None:
    """There is no turn to name, and a `chat/turnCancelled` without one is
    exactly the defect above."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        await chat.cancel()
        await asyncio.sleep(0.02)
    await host.stop()
    assert _dispatched(host) == []


# ── answering a tool call ────────────────────────────────────────────────────


def _recorder() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    sent: list[tuple[str, dict[str, Any]]] = []

    def dispatch(channel: str, action: Any) -> None:
        sent.append((channel, dict(action)))

    return sent, dispatch


def test_approving_a_tool_call_reaches_the_reducer() -> None:
    sent, dispatch = _recorder()
    ToolCallReady(
        {
            "channel": CHAT,
            "action": {
                "type": "chat/toolCallReady",
                "turnId": TURN,
                "toolCallId": "tc1",
                "invocationMessage": "Echoing",
            },
        },
        dispatch,
    ).approve()

    _, action = sent[-1]
    assert action["turnId"] == TURN
    # Required on the approved variant, and the only record of *how* the call
    # was allowed to run.
    assert action["confirmed"] == "user-action"

    call = _tool_call_after(_chat_with_tool_call("pending-confirmation"), action)
    assert call["status"] == "running"


def test_the_chosen_option_is_the_one_the_reducer_looks_for() -> None:
    """The reducer resolves `selectedOptionId` against the call's own options
    and stores the whole option. Under any other key the action still applies,
    and nothing anywhere records which button was pressed."""
    sent, dispatch = _recorder()
    ToolCallReady(
        {
            "channel": CHAT,
            "action": {
                "type": "chat/toolCallReady",
                "turnId": TURN,
                "toolCallId": "tc1",
                "invocationMessage": "Echoing",
            },
        },
        dispatch,
    ).approve(option_id="opt-1")

    _, action = sent[-1]
    options = [{"id": "opt-1", "label": "Approve in this session"}]
    call = _tool_call_after(_chat_with_tool_call("pending-confirmation", options=options), action)
    assert call["selectedOption"] == options[0]


def test_denying_a_tool_call_reaches_the_reducer() -> None:
    sent, dispatch = _recorder()
    ToolCallReady(
        {
            "channel": CHAT,
            "action": {
                "type": "chat/toolCallReady",
                "turnId": TURN,
                "toolCallId": "tc1",
                "invocationMessage": "Echoing",
            },
        },
        dispatch,
    ).deny(reason="skipped")

    _, action = sent[-1]
    call = _tool_call_after(_chat_with_tool_call("pending-confirmation"), action)
    assert call["status"] == "cancelled"
    assert call["reason"] == "skipped"


def test_a_result_review_reaches_the_reducer() -> None:
    sent, dispatch = _recorder()
    ToolCallResultReview(
        {"channel": CHAT, "action": {"turnId": TURN, "toolCallId": "tc1"}}, dispatch
    ).confirm()

    _, action = sent[-1]
    call = _tool_call_after(_chat_with_tool_call("pending-result-confirmation"), action)
    assert call["status"] == "completed"


def test_an_event_built_without_a_dispatcher_still_says_so_first() -> None:
    """The wiring bug is the caller's own; complaining about an id missing from
    the host's action would send them looking at the wrong peer."""
    with pytest.raises(RuntimeError, match="dispatcher"):
        ToolCallReady({"action": {"toolCallId": "x"}}).approve()


# ── a tool only this client can run ──────────────────────────────────────────


async def test_a_client_tool_result_is_not_thrown_away() -> None:
    """The blocker with the worst shape: the host resolves the provider's
    future off `toolCallId` alone, so the agent really did get the answer --
    while every transcript recorded the call as cancelled/`skipped`."""
    host = echo_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)

        async def usages(_action: dict[str, Any]) -> dict[str, Any]:
            return {
                "success": True,
                "pastTenseMessage": "Found 3 usages",
                "content": [{"type": "text", "text": "a.py:1 b.py:2 c.py:3"}],
            }

        tools.register({"name": "usages"}, usages)
        await tools.execute(
            CHAT,
            {
                "type": "chat/toolCallStart",
                "turnId": TURN,
                "toolCallId": "tc1",
                "toolName": "usages",
                "contributor": {"kind": "client", "clientId": client.client_id},
            },
        )
        await asyncio.sleep(0.02)
    await host.stop()

    action = _dispatched(host)[-1]
    assert action["type"] == "chat/toolCallComplete"
    running = _chat_with_tool_call("running", contributor={"kind": "client", "clientId": "me"})
    call = _tool_call_after(running, action)
    assert call["status"] == "completed"
    assert call["content"] == [{"type": "text", "text": "a.py:1 b.py:2 c.py:3"}]


async def test_the_responder_answers_the_entry_it_was_given() -> None:
    """`SessionState.inputNeeded` entries are specified to be self-sufficient --
    "every identifier needed to construct the response" -- and `turnId` is one
    of them, which is what makes answering without subscribing possible."""
    host = echo_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        responder = InputResponder(client.protocol, client.mirror)
        responder.confirm_tool(
            {
                "kind": "toolConfirmation",
                "id": f"{CHAT}#tc1",
                "chat": CHAT,
                "turnId": TURN,
                "toolCall": {"toolCallId": "tc1"},
            },
            approved=True,
        )
        await asyncio.sleep(0.02)
    await host.stop()

    action = _dispatched(host)[-1]
    call = _tool_call_after(_chat_with_tool_call("pending-confirmation"), action)
    assert call["status"] == "running"


# ── the constructor itself ───────────────────────────────────────────────────


def test_an_empty_identifier_is_refused_rather_than_sent() -> None:
    """A required parameter stops a call site forgetting the field; this stops
    it passing an empty one, which reduces to the same silent no-op."""
    with pytest.raises(ValueError, match="turnId"):
        actions.turn_cancelled("", duration_ms=1)
    with pytest.raises(ValueError, match="toolCallId"):
        actions.tool_call_complete(TURN, "", result={"success": True, "pastTenseMessage": "x"})


def test_optional_spec_fields_are_reachable_through_the_constructors() -> None:
    """The constructors deliberately take no `**extra`, so an optional field
    upstream defines is unreachable without a named parameter -- and the
    features behind them (`requiresResultConfirmation`, attachments, denial
    suggestions) simply do not exist through the typed surface."""
    started = actions.turn_started(
        TURN,
        text="hi",
        model="gpt-x",
        attachments=[{"uri": "file:///a.py"}],
        queued_message_id="q1",
    )
    # A bare-string model is wrapped: `Message.model` is a `ModelSelection`
    # object `{id, config?}`, and the bare string is wire-invalid.
    assert started["message"]["model"] == {"id": "gpt-x"}
    assert started["message"]["attachments"] == [{"uri": "file:///a.py"}]
    assert started["queuedMessageId"] == "q1"

    complete = actions.tool_call_complete(
        TURN,
        "tc1",
        result={"success": True, "pastTenseMessage": "x"},
        requires_result_confirmation=True,
    )
    assert complete["requiresResultConfirmation"] is True

    denied = actions.tool_call_denied(
        TURN,
        "tc1",
        user_suggestion={"text": "try Y instead", "origin": {"kind": "user"}},
        reason_message="too broad",
    )
    assert denied["userSuggestion"]["text"] == "try Y instead"
    assert denied["reasonMessage"] == "too broad"

    # And when not asked for, none of them appear -- absent, not null.
    bare = actions.turn_started(TURN, text="hi")
    assert "model" not in bare["message"]
    assert "attachments" not in bare["message"]
    assert "queuedMessageId" not in bare


def test_a_failed_tool_reports_a_result_the_schema_recognises() -> None:
    """`ToolCallResult` requires `success` and `pastTenseMessage`, its content
    blocks are keyed by `type`, and it has no `isError` -- that is MCP's
    spelling, and a result carrying it reads here as an unqualified success."""
    result = actions.tool_failure_result("tool exploded")
    assert result["success"] is False
    assert result["pastTenseMessage"]
    assert result["content"][0]["type"] == "text"
    assert "isError" not in result

    completed = _tool_call_after(
        _chat_with_tool_call("running"),
        actions.tool_call_complete(TURN, "tc1", result=result),
    )
    assert completed["status"] == "completed"
