"""The front door, driven end to end against a programmable host."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_host_client.api import (
    Delta,
    InputRequested,
    ToolCallReady,
    ToolCallRunning,
    TurnCompleted,
    TurnFailed,
    approve_all,
    auto,
    connect,
    deny_all,
    resolve_policy,
)
from agent_host_client.client.errors import AhpClientError
from agent_host_client.testing import FakeHost, FakeToolCall, echo_host

CHAT = "ahp-chat://c/s"

SESSION_STATE = {
    "lifecycle": "ready",
    "defaultChat": CHAT,
    "interactivity": "full",
}


def _session_host(**overrides: Any) -> FakeHost:
    host = echo_host()
    state = {**SESSION_STATE, **overrides}

    def subscribe(params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        if channel.startswith("ahp-chat:"):
            body: Any = {"turns": [], "activeTurn": None}
        elif channel == "ahp-root://":
            body = host.root_state
        else:
            body = state
        # `fromSeq` is the last number ISSUED. Reporting the *next* one makes
        # the first action look already-included, and a client that honours the
        # ordering rule correctly then drops it -- which is what this helper did
        # on its first draft, and what the mirror caught.
        return {"snapshot": {"resource": channel, "state": body, "fromSeq": host._server_seq}}

    host.on("subscribe", subscribe)
    host.on("createSession", lambda _p: {})
    host.on("disposeSession", lambda _p: {})
    return host


# ── approvals ────────────────────────────────────────────────────────────────


def _ready(name: str = "", annotations: dict[str, Any] | None = None) -> ToolCallReady:
    """A ready event exactly as `event_for` builds one.

    The name and the hints are **fields**, resolved from state, because
    `chat/toolCallReady` carries neither: `toolName` is published once on
    `chat/toolCallStart`, and `annotations` is a property of `ToolDefinition`.
    Assembling these out of the action -- as these tests once did -- agrees with
    an implementation that reads the same non-existent keys, and both disagree
    with every real host.
    """
    return ToolCallReady(
        {"channel": CHAT, "action": {"type": "chat/toolCallReady", "toolCallId": "tc1"}},
        None,
        name,
        annotations,
    )


async def test_manual_is_the_default_and_does_not_silently_approve() -> None:
    """Silently approving is the obvious wrong default; silently approving *is*
    what a permissive default would do."""
    policy = resolve_policy(None)
    assert await policy(_ready("rm")) is False


async def test_string_shorthands_resolve() -> None:
    call = _ready("read")
    assert await resolve_policy("all")(call) is True
    assert await resolve_policy("none")(call) is False
    with pytest.raises(ValueError, match="unknown approval policy"):
        resolve_policy("sometimes")


async def test_reads_really_inspects_the_read_only_hint() -> None:
    """`ahpx`'s equivalent silently degrades to prompting for everything, and
    its own docs say otherwise."""
    policy = auto(read_only=True)
    assert await policy(_ready("cat", {"readOnlyHint": True})) is True
    assert await policy(_ready("rm", {"readOnlyHint": False})) is False
    # Falls back, does not guess.
    assert await policy(_ready("mystery")) is False


async def test_allow_and_deny_lists_win_over_the_hint() -> None:
    policy = auto(allow=["safe"], deny=["dangerous"], read_only=True)
    assert await policy(_ready("dangerous", {"readOnlyHint": True})) is False
    assert await policy(_ready("safe")) is True


# ── connect ──────────────────────────────────────────────────────────────────


async def test_connect_requires_exactly_one_target() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        connect()

    async def factory() -> Any:  # pragma: no cover - never called
        raise AssertionError("unreachable")

    with pytest.raises(ValueError, match="exactly one"):
        connect("ws://x", transport_factory=factory)


async def test_connect_over_a_supplied_transport() -> None:
    host = echo_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        assert client.protocol_version == "0.7.0"
        assert client.client_id
        assert client.root["agents"] == [{"provider": "echo", "displayName": "Echo"}]
    await host.stop()


async def test_connect_is_awaitable_as_well_as_a_context_manager() -> None:
    host = echo_host()
    await host.start()
    client = await connect(transport=host.transport())
    assert client.protocol_version == "0.7.0"
    await client.aclose()
    await host.stop()


async def test_the_raw_protocol_is_always_reachable() -> None:
    """`client.protocol.request(...)` must work for a method we have never heard
    of, or this library becomes a ceiling."""
    host = echo_host()
    await host.start()
    host.on("someFutureMethod", lambda _p: {"ok": True})
    async with connect(transport=host.transport()) as client:
        assert await client.protocol.request("someFutureMethod", {}) == {"ok": True}
    await host.stop()


# ── sessions ─────────────────────────────────────────────────────────────────


async def test_create_session_mints_a_provider_scoped_uri() -> None:
    """`<provider>:/<uuid>`, matching VS Code -- not `ahp-session:`."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo", cwd=".")
        assert session.uri.startswith("echo:/")
        created = next(m for m in host.received if m.get("method") == "createSession")
        assert created["params"]["channel"] == session.uri
        # Plural since 0.7.0.
        assert "workingDirectories" in created["params"]
    await host.stop()


async def test_a_provisional_session_returns_immediately() -> None:
    """Waiting unconditionally deadlocks for the full timeout against a host
    that answers `lifecycle: "creating"`."""
    host = _session_host(lifecycle="creating")
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await asyncio.wait_for(
            client.create_session(provider="echo", ready_timeout=30.0), 2
        )
        assert session.state["lifecycle"] == "creating"
    await host.stop()


async def test_progress_mints_a_token_because_otherwise_nothing_fires() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        await client.create_session(provider="echo", progress=True)
        created = next(m for m in host.received if m.get("method") == "createSession")
        assert created["params"]["progressToken"]
    await host.stop()


async def test_a_session_we_did_not_create_is_never_disposed_on_exit() -> None:
    """Disposing someone else's session on a `with` exit is the kind of surprise
    that loses trust."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        async with await client.open_session("echo:/theirs"):
            pass
        assert not [m for m in host.received if m.get("method") == "disposeSession"]
    await host.stop()


async def test_a_read_only_chat_refuses_to_send() -> None:
    """`ChatState.interactivity` is undocumented in every guide and gates
    whether a client may send at all."""
    host = _session_host(interactivity="read-only")
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        with pytest.raises(AhpClientError, match="read-only"):
            await session.prompt("hi")
    await host.stop()


# ── turns ────────────────────────────────────────────────────────────────────


async def test_a_turn_streams_deltas_and_ends_with_authoritative_text() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        stream = chat.prompt("hello", turn_id="t1")
        seen: list[str] = []

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(chat.uri, "t1", text="BANANA")

        driver = asyncio.get_running_loop().create_task(drive())
        async for event in stream:
            if isinstance(event, Delta):
                seen.append(event.text)
        await driver
        assert "".join(seen) == "BANANA"
    await host.stop()


async def test_a_folded_first_delta_is_recovered_from_the_mirror() -> None:
    """Hosts fold a turn's opening characters into the response part instead of
    emitting them, so a consumer that renders only deltas shows "ANANA"."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        stream = chat.prompt("hello", turn_id="t1")

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(chat.uri, "t1", text="BANANA", fold_first_delta=True)

        driver = asyncio.get_running_loop().create_task(drive())
        deltas: list[str] = []
        async for event in stream:
            if isinstance(event, Delta):
                deltas.append(event.text)
        await driver
        # The deltas alone are missing the opening character...
        assert "".join(deltas) == "ANANA"
        # ...and the authoritative text is not.
        assert stream.text() == "BANANA"
    await host.stop()


async def test_awaiting_a_turn_drains_it_and_returns_the_final_event() -> None:
    """One object, two idioms, one code path."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(chat.uri, "t1", text="done")

        driver = asyncio.get_running_loop().create_task(drive())
        result = await chat.prompt("hello", turn_id="t1", approvals="all")
        await driver
        assert isinstance(result, TurnCompleted)
        assert result.text == "done"
    await host.stop()


async def test_a_tool_call_can_be_approved_from_the_event_itself() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(
                chat.uri,
                "t1",
                text="ok",
                tools=[FakeToolCall("tc1", "read_file", confirmed=True)],
            )

        driver = asyncio.get_running_loop().create_task(drive())
        approved = False
        async for event in chat.prompt("hello", turn_id="t1"):
            if isinstance(event, ToolCallReady):
                event.approve()
                approved = True
        await driver
        assert approved
        confirmations = [
            m
            for m in host.received
            if m.get("method") == "dispatchAction"
            and m["params"]["action"]["type"] == "chat/toolCallConfirmed"
        ]
        assert confirmations[-1]["params"]["action"]["approved"] is True
    await host.stop()


async def test_an_event_without_a_dispatcher_says_so_instead_of_failing_quietly() -> None:
    call = ToolCallReady({"action": {"toolCallId": "x"}})
    with pytest.raises(RuntimeError, match="dispatcher"):
        call.approve()


def _confirmations(host: FakeHost) -> list[dict[str, Any]]:
    return [
        m["params"]["action"]
        for m in host.received
        if m.get("method") == "dispatchAction"
        and m["params"]["action"]["type"] == "chat/toolCallConfirmed"
    ]


async def test_a_policy_switching_on_the_tool_name_is_given_one() -> None:
    """The name is nowhere on the action a policy is answering; it is on
    `chat/toolCallStart` and on the tool call's own state. Read off the ready
    action it is always "", so every `allow=` list falls through to `otherwise`
    and denies -- silently, and looking like a deliberate decision."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(
                chat.uri, "t1", text="ok", tools=[FakeToolCall("tc1", "read_file", confirmed=True)]
            )

        driver = asyncio.get_running_loop().create_task(drive())
        await chat.prompt("hello", turn_id="t1", approvals=auto(allow=["read_file"]))
        await driver
        await asyncio.sleep(0.05)
        assert [c["approved"] for c in _confirmations(host)] == [True]
    await host.stop()


async def test_reads_finds_the_hint_on_the_tool_definition() -> None:
    """`annotations` is a property of `ToolDefinition` -- published in
    `SessionState.serverTools` and `activeClients[].tools`, and on no `chat/*`
    action ever. Looking for it on the action makes `approvals="reads"` deny
    every read-only tool, the exact opposite of what it is named for."""
    host = _session_host(
        serverTools=[
            {"name": "read_file", "annotations": {"readOnlyHint": True, "title": "Read File"}}
        ]
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(
                chat.uri, "t1", text="ok", tools=[FakeToolCall("tc1", "read_file", confirmed=True)]
            )

        driver = asyncio.get_running_loop().create_task(drive())
        await chat.prompt("hello", turn_id="t1", approvals="reads")
        await driver
        await asyncio.sleep(0.05)
        assert [c["approved"] for c in _confirmations(host)] == [True]
    await host.stop()


async def test_an_auto_confirmed_tool_call_is_never_answered() -> None:
    """A `chat/toolCallReady` carrying `confirmed` has already transitioned to
    `running`. The host emits one for every call it does not gate, so answering
    them means dispatching a confirmation per tool call on the ordinary path --
    each of which the host refuses with "no tool call awaiting that id"."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
            await host.push(
                chat.uri,
                {
                    "type": "chat/toolCallStart",
                    "turnId": "t1",
                    "toolCallId": "tc1",
                    "toolName": "read_file",
                    "displayName": "Read File",
                },
            )
            await host.push(
                chat.uri,
                {
                    "type": "chat/toolCallReady",
                    "turnId": "t1",
                    "toolCallId": "tc1",
                    "invocationMessage": "Reading",
                    "confirmed": "not-needed",
                },
            )
            await host.push(chat.uri, {"type": "chat/turnComplete", "turnId": "t1"})

        driver = asyncio.get_running_loop().create_task(drive())
        seen: list[Any] = []
        async for event in chat.prompt("hello", turn_id="t1", approvals="all"):
            seen.append(event)
        await driver
        await asyncio.sleep(0.05)
        running = [e for e in seen if isinstance(e, ToolCallRunning)]
        assert [e.tool_name for e in running] == ["read_file"]
        assert not [e for e in seen if isinstance(e, ToolCallReady)]
        assert _confirmations(host) == []
    await host.stop()


async def test_an_elicitation_is_completed_through_the_front_door() -> None:
    """The whole elicitation surface: without `chat/inputCompleted` the host
    stays parked on the request it opened and the turn never ends. The id is at
    `request.id` -- there is no `requestId` on the action -- and the questions
    are the only thing a consumer can render the prompt from."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        request = {
            "id": "in-1",
            "message": "Echo it back how?",
            "questions": [
                {
                    "id": "style",
                    "kind": "single-select",
                    "message": "Style?",
                    "options": [{"id": "shout", "label": "SHOUT"}],
                }
            ],
        }

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
            await host.push(
                chat.uri, {"type": "chat/inputRequested", "turnId": "t1", "request": request}
            )
            await asyncio.sleep(0.1)
            await host.push(chat.uri, {"type": "chat/turnComplete", "turnId": "t1"})

        driver = asyncio.get_running_loop().create_task(drive())
        answered: list[InputRequested] = []
        async for event in chat.prompt("hello", turn_id="t1"):
            if isinstance(event, InputRequested):
                answered.append(event)
                event.answer({"style": "shout"})
        await driver
        await asyncio.sleep(0.05)
        assert [e.request_id for e in answered] == ["in-1"]
        assert [q["kind"] for q in answered[0].questions] == ["single-select"]
        completions = [
            m["params"]["action"]
            for m in host.received
            if m.get("method") == "dispatchAction"
            and m["params"]["action"]["type"] == "chat/inputCompleted"
        ]
        assert completions == [
            {
                "type": "chat/inputCompleted",
                "requestId": "in-1",
                "response": "accept",
                "answers": {
                    "style": {"state": "submitted", "value": {"kind": "selected", "value": "shout"}}
                },
            }
        ]
    await host.stop()


async def test_events_from_another_turn_are_filtered_out() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(chat.uri, "other", text="NOISE")
            await host.emit_turn(chat.uri, "t1", text="MINE")

        driver = asyncio.get_running_loop().create_task(drive())
        deltas: list[str] = []
        async for event in chat.prompt("hello", turn_id="t1"):
            if isinstance(event, Delta):
                deltas.append(event.text)
        await driver
        assert "".join(deltas) == "MINE"
    await host.stop()


async def test_a_disposed_session_ends_the_turn_rather_than_hanging() -> None:
    """The host drops the chat channel, so the `chat/turnComplete` this stream
    waits for has nowhere left to arrive -- not even from a host that publishes
    a closing action. `root/sessionRemoved` is the only notice, and it is a
    notification rather than an envelope, so the stream filtered it out and
    blocked for the whole `idle_timeout`."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        stream = chat.prompt("hello", turn_id="t1", idle_timeout=None)

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
            await host.notify(
                "root/sessionRemoved", {"channel": "ahp-root://", "session": session.uri}
            )

        driver = asyncio.get_running_loop().create_task(drive())
        outcome = await asyncio.wait_for(stream, 5)
        await driver
        assert isinstance(outcome, TurnFailed)
        assert "disposed" in outcome.reason
    await host.stop()


async def test_a_disposed_chat_ends_the_turn_rather_than_hanging() -> None:
    """`session/chatRemoved` is an action on the *session* channel, which the
    stream's chat-URI filter discards -- the same hang by the other door."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        stream = chat.prompt("hello", turn_id="t1", idle_timeout=None)

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
            await host.push(session.uri, {"type": "session/chatRemoved", "chat": chat.uri})

        driver = asyncio.get_running_loop().create_task(drive())
        outcome = await asyncio.wait_for(stream, 5)
        await driver
        assert isinstance(outcome, TurnFailed)
        assert chat.uri in outcome.reason
    await host.stop()


async def test_connect_raises_when_the_handshake_is_permanently_refused() -> None:
    """The whole front door goes through `start(wait=True)`. A -32005 left it
    unsatisfiable, so `async with connect(...)` never returned and never
    raised."""
    from agent_host_client.client.errors import UnsupportedProtocolVersion
    from agent_host_client.testing import FakeRpcError

    host = echo_host()

    def refuse(_p: Any) -> Any:
        raise FakeRpcError({"code": -32005, "message": "no mutually supported version"})

    host.on("initialize", refuse)
    await host.start()
    with pytest.raises(UnsupportedProtocolVersion):
        async with await asyncio.wait_for(connect(transport=host.transport()), 5):
            pass  # pragma: no cover - the handshake never completes
    await host.stop()


async def test_an_idle_turn_times_out_with_a_specific_message() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        stream = chat.prompt("hello", turn_id="t1", idle_timeout=0.05)
        with pytest.raises(TimeoutError, match="t1"):
            async for _event in stream:
                pass
    await host.stop()


async def test_a_failing_turn_ends_the_stream_rather_than_idling_out() -> None:
    """`chat/error` is the only failure action the spec defines. Mapped to
    nothing it is indistinguishable from a host that went quiet, and the caller
    waits out the full `idle_timeout` for a turn that is already over."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
            await host.push(
                chat.uri,
                {
                    "type": "chat/error",
                    "turnId": "t1",
                    "duration": 3,
                    "error": {"errorType": "agent.turn", "message": "RuntimeError: kaboom"},
                },
            )

        driver = asyncio.get_running_loop().create_task(drive())
        # An idle timeout far under the default, so a regression fails in a
        # second rather than stalling the suite for five minutes.
        result = await chat.prompt("hello", turn_id="t1", idle_timeout=1.0)
        await driver
        assert isinstance(result, TurnFailed)
        assert result.reason == "RuntimeError: kaboom"
        assert result.error_type == "agent.turn"
    await host.stop()


async def test_a_rejected_turn_start_fails_instead_of_looking_like_success() -> None:
    """The host refuses a second concurrent turn by echoing the action back with
    a `rejectionReason`. Handing the caller a `TurnStarted` for a turn that will
    never run is worse than the hang that follows it."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.push(
                chat.uri,
                {"type": "chat/turnStarted", "turnId": "t2"},
                origin={"clientId": client.client_id, "clientSeq": 1},
                rejection="a turn is already active",
            )

        driver = asyncio.get_running_loop().create_task(drive())
        events: list[Any] = []
        async for event in chat.prompt("second", turn_id="t2", idle_timeout=1.0):
            events.append(event)
        await driver
        assert len(events) == 1
        assert isinstance(events[0], TurnFailed)
        assert events[0].reason == "a turn is already active"
    await host.stop()


async def test_a_rejected_confirmation_does_not_end_the_turn() -> None:
    """Another client answered the approval first. The mirror reverts our
    optimistic effect and the turn carries on -- exactly the case
    `ToolCallReady`'s docstring calls "not an error"."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
            await host.push(
                chat.uri,
                {"type": "chat/toolCallConfirmed", "turnId": "t1", "toolCallId": "tc1"},
                origin={"clientId": client.client_id, "clientSeq": 1},
                rejection="already answered",
            )
            await host.push(chat.uri, {"type": "chat/turnComplete", "turnId": "t1"})

        driver = asyncio.get_running_loop().create_task(drive())
        result = await chat.prompt("hello", turn_id="t1", idle_timeout=1.0)
        await driver
        assert isinstance(result, TurnCompleted)
    await host.stop()


def test_policies_are_plain_callables_so_a_caller_can_write_their_own() -> None:
    assert callable(approve_all())
    assert callable(deny_all())
