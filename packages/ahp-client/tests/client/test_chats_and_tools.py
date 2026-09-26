"""The command surface the interop run could not reach through the typed API.

Three defects and one gap, all found by driving a real host: `createChat` had no
working call shape at all, the tools a session publishes had no executor behind
them, `terminalCommandPrefix` was absorbed by nobody, and multi-chat -- the one
surface of the four with no typed API whose *command* the client already ships --
had to be driven through `client.protocol.request`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from ahp_protocol.conformance.corpus import CORPUS_ROOT

from ahp_client.api import connect
from ahp_client.client.errors import AhpClientError
from ahp_client.serve import ClientToolHost
from ahp_client.testing import FakeHost, FakeToolCall, echo_host

CHAT = "ahp-chat://c/s"

#: The vendored schema, not a transcription of it. A test that restates a
#: `required` list agrees with whatever the implementation was reading.
SCHEMA = json.loads((CORPUS_ROOT / "schema" / "commands.schema.json").read_text(encoding="utf-8"))


def _session_host(*, capabilities: dict[str, Any] | None = None, **overrides: Any) -> FakeHost:
    host = FakeHost(
        agents=[
            {
                "provider": "echo",
                "displayName": "Echo",
                **({"capabilities": capabilities} if capabilities is not None else {}),
            }
        ]
    )
    state = {"lifecycle": "ready", "defaultChat": CHAT, "interactivity": "full", **overrides}

    def subscribe(params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        if channel.startswith("ahp-chat:"):
            body: Any = {"turns": [], "activeTurn": None}
        elif channel == "ahp-root://":
            body = host.root_state
        else:
            body = state
        return {"snapshot": {"resource": channel, "state": body, "fromSeq": host._server_seq}}

    host.on("initialize", lambda params: _initialize(host, params))
    host.on("ping", lambda _p: {})
    host.on("listSessions", lambda _p: {"items": []})
    host.on("subscribe", subscribe)
    host.on("createSession", lambda _p: {})
    host.on("disposeSession", lambda _p: {})
    host.on("createChat", lambda _p: {})
    host.on("disposeChat", lambda _p: {})
    return host


def _initialize(host: FakeHost, params: dict[str, Any]) -> dict[str, Any]:
    """Handshake carrying every advertisement a client is expected to keep."""
    snapshots = [
        {"resource": uri, "state": host.root_state, "fromSeq": host._server_seq}
        for uri in params.get("initialSubscriptions") or []
        if uri == "ahp-root://"
    ]
    return {
        "protocolVersion": host.protocol_version,
        "serverSeq": host._server_seq,
        "snapshots": snapshots,
        "completionTriggerCharacters": ["@", "/"],
        "terminalCommandPrefix": "!",
        "defaultDirectory": "file:///work",
    }


def _sent(host: FakeHost, method: str) -> list[dict[str, Any]]:
    return [m["params"] for m in host.received if m.get("method") == method]


def _dispatched(host: FakeHost, action_type: str) -> list[dict[str, Any]]:
    return [
        m["params"]["action"]
        for m in host.received
        if m.get("method") == "dispatchAction" and m["params"]["action"]["type"] == action_type
    ]


def _denials(host: FakeHost) -> list[dict[str, Any]]:
    """A denial is a `chat/toolCallConfirmed` with `approved: false` -- there is
    no `chat/toolCallDenied` action, and a test looking for one finds nothing
    whatever the client sent."""
    return [a for a in _dispatched(host, "chat/toolCallConfirmed") if a["approved"] is False]


async def _settle(predicate: Any, timeout: float = 2.0) -> None:
    """Wait for a background pump rather than sleeping a guessed interval."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


# ── createChat ───────────────────────────────────────────────────────────────


async def test_create_chat_sends_both_uris_and_they_are_not_the_same_one() -> None:
    """`CreateChatParams` requires `channel` AND `chat`, and they are different
    things: the session that will contain the chat, and the chat's own URI. The
    wrapper took one argument and `_scoped` wrote it into `channel`, so no
    `chat` key was ever emitted and every call shape answered -32602."""
    host = _session_host(capabilities={"multipleChats": {}})
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.create_chat()
        params = _sent(host, "createChat")[-1]
        assert params["channel"] == session.uri
        assert params["chat"] == chat.uri
        required = SCHEMA["$defs"]["CreateChatParams"]["required"]
        assert [key for key in required if key not in params] == []
    await host.stop()


async def test_create_chat_carries_the_optional_fields_it_is_given() -> None:
    # `workingDirectories` needs its own advertisement: "A client MUST NOT
    # supply this field unless the agent advertises
    # `AgentCapabilities.multipleWorkingDirectories`."
    host = _session_host(
        capabilities={"multipleChats": {"fork": True}, "multipleWorkingDirectories": {}}
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        await session.create_chat(
            initial_message="pick up from here",
            source={"kind": "fork", "chat": CHAT, "turnId": "t1"},
            working_directories=["file:///work"],
        )
        params = _sent(host, "createChat")[-1]
        # The string convenience carries the required `origin` too: `Message`
        # requires `[text, origin]`, and "a client is only allowed to send
        # `MessageKind.User` messages" -- an origin-less Message is republished
        # verbatim inside the host's `chat/turnStarted` and frozen into every
        # peer's transcript.
        assert params["initialMessage"] == {
            "text": "pick up from here",
            "origin": {"kind": "user"},
        }
        assert params["source"]["kind"] == "fork"
        assert params["workingDirectories"] == ["file:///work"]
    await host.stop()


async def test_working_directories_on_create_chat_need_their_own_advertisement() -> None:
    """The same told-in-advance MUST NOT as `multipleChats`, gated the same
    way -- and a presence flag, so `{}` advertises support."""
    host = _session_host(capabilities={"multipleChats": {}})
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        with pytest.raises(AhpClientError, match="multipleWorkingDirectories"):
            await session.create_chat(working_directories=["file:///work"])
        assert _sent(host, "createChat") == []
    await host.stop()


async def test_a_second_working_directory_on_create_session_needs_the_advertisement() -> None:
    """ "When absent, clients ... MUST NOT set more than one entry in
    `CreateSessionParams.workingDirectories`" -- refused locally, like the
    sibling MUST NOTs, rather than making the host enforce a rule we were told
    about in advance. One entry stays fine: that is the pre-capability shape."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        with pytest.raises(AhpClientError, match="multipleWorkingDirectories"):
            await client.create_session(
                provider="echo", working_directories=["file:///a", "file:///b"]
            )
        assert _sent(host, "createSession") == []
        await client.create_session(provider="echo", working_directories=["file:///a"])
        assert _sent(host, "createSession")[-1]["workingDirectories"] == ["file:///a"]
    await host.stop()


async def test_multi_chat_is_refused_when_the_agent_never_advertised_it() -> None:
    """ "When absent, clients MUST NOT call `createChat` to open chats beyond the
    default one the session starts with." A MUST NOT we were told about in
    advance is not something to make the host enforce."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        with pytest.raises(AhpClientError, match="multipleChats"):
            await session.create_chat()
        assert _sent(host, "createChat") == []
    await host.stop()


async def test_an_empty_capability_object_advertises_support() -> None:
    """`multipleChats: {}` is the ordinary advertisement and `{}` is **falsy in
    Python**, so a truthiness test reads every plain multi-chat agent as
    unsupported -- the presence-flag trap `docs/plan.md` §9 names."""
    host = _session_host(capabilities={"multipleChats": {}})
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        assert session.capabilities["multipleChats"] == {}
        await session.create_chat()
        assert len(_sent(host, "createChat")) == 1
    await host.stop()


async def test_fork_and_side_chat_are_each_their_own_opt_in() -> None:
    host = _session_host(capabilities={"multipleChats": {"fork": True}})
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        with pytest.raises(AhpClientError, match="sideChat"):
            await session.create_chat(source={"kind": "sideChat", "chat": CHAT, "turnId": "t1"})
        await session.create_chat(source={"kind": "fork", "chat": CHAT, "turnId": "t1"})
    await host.stop()


async def test_a_created_chat_is_subscribed_and_disposable() -> None:
    host = _session_host(capabilities={"multipleChats": {}})
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.create_chat()
        assert chat.uri in [p["channel"] for p in _sent(host, "subscribe")]
        await chat.dispose()
        assert _sent(host, "disposeChat")[-1]["channel"] == chat.uri
    await host.stop()


async def test_chats_lists_the_catalogue_without_subscribing_to_any_of_them() -> None:
    host = _session_host(
        capabilities={"multipleChats": {}},
        chats=[
            {"resource": CHAT, "title": "First"},
            {"resource": "ahp-chat://c/2", "title": "Two"},
        ],
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        assert [c["title"] for c in session.chats()] == ["First", "Two"]
        subscribed = [p["channel"] for p in _sent(host, "subscribe")]
        assert "ahp-chat://c/2" not in subscribed
        second = await session.open_chat("ahp-chat://c/2")
        assert "ahp-chat://c/2" in [p["channel"] for p in _sent(host, "subscribe")]
        assert second.uri == "ahp-chat://c/2"
    await host.stop()


# ── the tools we publish ─────────────────────────────────────────────────────


async def test_a_published_tool_is_actually_executed() -> None:
    """`create_session(tools=…)` advertised tools on `session/activeClientSet`
    and then no code path anywhere ran one: `ClientToolHost` was referenced by
    nothing outside tests. The host parks the turn on a future nobody resolves
    and the agent waits forever."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)
        seen: list[Any] = []

        async def usages(action: dict[str, Any]) -> dict[str, Any]:
            seen.append(action.get("toolInput"))
            return {"content": [{"type": "text", "text": "3 usages found"}], "success": True}

        tools.register({"name": "usages", "description": "find usages"}, usages)
        session = await client.create_session(provider="echo", tools=tools)
        chat = await session.chat()

        await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
        await host._emit_tool(
            chat.uri,
            "t1",
            FakeToolCall(
                "tc1",
                "usages",
                contributor={"kind": "client", "clientId": client.client_id},
                tool_input={"symbol": "connect"},
            ),
        )
        await _settle(lambda: _dispatched(host, "chat/toolCallComplete"))

        completion = _dispatched(host, "chat/toolCallComplete")[-1]
        # `turnId` is required and the reducer no-ops without it: the tool ran,
        # the agent used its output, and every transcript showed the call
        # cancelled as `skipped`.
        assert completion["turnId"] == "t1"
        assert completion["toolCallId"] == "tc1"
        assert completion["result"]["success"] is True
        assert seen == [{"symbol": "connect"}]
    await host.stop()


async def test_a_session_opened_with_tools_offers_and_runs_them() -> None:
    """`open_session(tools=...)` is how a client that restarted takes a
    session's tools up again: it joins as an active client offering them, and
    runs their calls, as if it had created the session."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)
        tools.register({"name": "usages"}, lambda _a: _ok())
        session = await client.open_session("echo:/theirs", tools=tools)
        await _settle(lambda: _dispatched(host, "session/activeClientSet"))
        [joined] = _dispatched(host, "session/activeClientSet")
        assert joined["activeClient"]["clientId"] == client.client_id
        assert [t["name"] for t in joined["activeClient"]["tools"]] == ["usages"]

        chat = await session.chat()
        await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
        await host._emit_tool(
            chat.uri,
            "t1",
            FakeToolCall(
                "tc1", "usages", contributor={"kind": "client", "clientId": client.client_id}
            ),
        )
        await _settle(lambda: _dispatched(host, "chat/toolCallComplete"))
        assert _dispatched(host, "chat/toolCallComplete")[-1]["result"]["success"] is True
        await session._stop_tools()
    await host.stop()


async def test_the_executor_is_found_by_a_name_the_ready_action_does_not_carry() -> None:
    """`toolName` is published once, on `chat/toolCallStart`; the ready that
    hands execution over carries none, and neither does it name the contributor
    on every host. Resolving both off the ready action alone denies every call
    as unregistered -- so the pump reads the call's *state*, and this drives it
    with the two fields present only there."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)
        tools.register({"name": "usages"}, lambda _a: _ok())
        session = await client.create_session(provider="echo", tools=tools)
        chat = await session.chat()
        await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
        await host.push(
            chat.uri,
            {
                "type": "chat/toolCallStart",
                "turnId": "t1",
                "toolCallId": "tc1",
                "toolName": "usages",
                "contributor": {"kind": "client", "clientId": client.client_id},
            },
        )
        await host.push(
            chat.uri,
            {
                "type": "chat/toolCallReady",
                "turnId": "t1",
                "toolCallId": "tc1",
                "invocationMessage": "Running usages",
                "confirmed": "not-needed",
            },
        )
        await _settle(lambda: _dispatched(host, "chat/toolCallComplete"))
        assert _denials(host) == []
    await host.stop()


async def _ok() -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "ok"}], "success": True}


async def test_a_tool_advertised_without_an_executor_is_denied_not_dropped() -> None:
    """Advertised-and-absent is worse than absent: the agent asks, nothing
    answers, and the turn never ends. A denial is an answer."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo", tools=[{"name": "usages"}])
        created = _sent(host, "createSession")[-1]
        assert created["activeClient"]["tools"] == [{"name": "usages"}]
        chat = await session.chat()
        await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
        # `clientId` is required on `ToolCallClientContributor`, and `owns()`
        # only answers calls addressed to this client by id.
        await host._emit_tool(
            chat.uri,
            "t1",
            FakeToolCall(
                "tc1", "usages", contributor={"kind": "client", "clientId": client.client_id}
            ),
        )
        await _settle(lambda: _denials(host))
        denial = _denials(host)[-1]
        assert denial["turnId"] == "t1"
        assert denial["toolCallId"] == "tc1"
    await host.stop()


async def test_another_clients_tool_call_is_left_alone() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)
        tools.register({"name": "usages"}, lambda _a: _ok())
        session = await client.create_session(provider="echo", tools=tools)
        chat = await session.chat()
        await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
        await host._emit_tool(
            chat.uri,
            "t1",
            FakeToolCall("tc1", "usages", contributor={"kind": "client", "clientId": "somebody"}),
        )
        await asyncio.sleep(0.1)
        assert _dispatched(host, "chat/toolCallComplete") == []
        assert _denials(host) == []
    await host.stop()


async def test_a_republished_ready_does_not_run_the_tool_twice() -> None:
    """A second `chat/toolCallReady` is the one action that reaches an
    already-running call; hosts send it to revise the invocation message."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)
        runs = 0

        async def counted(_action: dict[str, Any]) -> dict[str, Any]:
            nonlocal runs
            runs += 1
            return await _ok()

        tools.register({"name": "usages"}, counted)
        session = await client.create_session(provider="echo", tools=tools)
        chat = await session.chat()
        await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
        call = FakeToolCall(
            "tc1", "usages", contributor={"kind": "client", "clientId": client.client_id}
        )
        await host._emit_tool(chat.uri, "t1", call)
        await _settle(lambda: runs == 1)
        await host._emit_tool(chat.uri, "t1", call)
        await asyncio.sleep(0.1)
        assert runs == 1
    await host.stop()


async def test_a_client_contributor_without_a_client_id_is_not_ours() -> None:
    """`ToolCallClientContributor` requires both `kind` and `clientId`, so a
    kind=client contributor without one is malformed data, not a broadcast --
    treating it as "whichever client is active, which is us" made every active
    client execute or deny the same call."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)
        tools.register({"name": "usages"}, lambda _a: _ok())
        session = await client.create_session(provider="echo", tools=tools)
        chat = await session.chat()
        await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
        await host._emit_tool(
            chat.uri, "t1", FakeToolCall("tc1", "usages", contributor={"kind": "client"})
        )
        await asyncio.sleep(0.1)
        assert _dispatched(host, "chat/toolCallComplete") == []
        assert _denials(host) == []
    await host.stop()


async def test_a_gated_client_call_waits_for_approval_and_then_runs() -> None:
    """Only *typically* does a host auto-confirm a client tool. A ready
    without `confirmed` leaves the call `pending-confirmation` -- not handed
    over, and running it would execute a tool nobody approved. The approval
    that later hands it over arrives as `chat/toolCallConfirmed` (there is no
    second ready), which carries neither `toolName` nor `toolInput` -- the
    mirror has both."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)
        seen: list[Any] = []

        async def usages(action: dict[str, Any]) -> dict[str, Any]:
            seen.append(action.get("toolInput"))
            return await _ok()

        tools.register({"name": "usages"}, usages)
        session = await client.create_session(provider="echo", tools=tools)
        chat = await session.chat()
        await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
        await host.push(
            chat.uri,
            {
                "type": "chat/toolCallStart",
                "turnId": "t1",
                "toolCallId": "tc1",
                "toolName": "usages",
                "contributor": {"kind": "client", "clientId": client.client_id},
            },
        )
        # Gated: no `confirmed`, so the reducer holds the call at
        # `pending-confirmation`, never `running`.
        await host.push(
            chat.uri,
            {
                "type": "chat/toolCallReady",
                "turnId": "t1",
                "toolCallId": "tc1",
                "invocationMessage": "Running usages",
                "toolInput": '{"symbol": "connect"}',
            },
        )
        await asyncio.sleep(0.15)
        assert seen == []
        assert _dispatched(host, "chat/toolCallComplete") == []
        # Somebody approves -- possibly on another client entirely.
        await host.push(
            chat.uri,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": "t1",
                "toolCallId": "tc1",
                "approved": True,
                "confirmed": "user-action",
            },
        )
        await _settle(lambda: _dispatched(host, "chat/toolCallComplete"))
        # The input reached the executor from state, and exactly once.
        assert seen == ['{"symbol": "connect"}']
        completion = _dispatched(host, "chat/toolCallComplete")[-1]
        assert completion["turnId"] == "t1"
        assert completion["toolCallId"] == "tc1"
    await host.stop()


async def test_two_sessions_on_one_connection_do_not_double_run_a_call() -> None:
    """`owns()` matches on `clientId`, which is identical for every session
    this `Client` created -- so only the per-session chat scoping keeps two
    pumps from both answering one call, double-executing the tool and
    overwriting the first `chat/toolCallComplete` with the second."""
    host = FakeHost(agents=[{"provider": "echo", "displayName": "Echo"}])
    chat_of = {"echo:/a": "ahp-chat://a", "echo:/b": "ahp-chat://b"}

    def subscribe(params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        if channel.startswith("ahp-chat:"):
            body: Any = {"turns": [], "activeTurn": None}
        elif channel == "ahp-root://":
            body = host.root_state
        else:
            body = {
                "lifecycle": "ready",
                "defaultChat": chat_of[channel],
                "interactivity": "full",
            }
        return {"snapshot": {"resource": channel, "state": body, "fromSeq": host._server_seq}}

    host.on("initialize", lambda params: _initialize(host, params))
    host.on("listSessions", lambda _p: {"items": []})
    host.on("subscribe", subscribe)
    host.on("createSession", lambda _p: {})
    host.on("disposeSession", lambda _p: {})
    await host.start()
    async with connect(transport=host.transport()) as client:
        runs: list[str] = []

        def tool_host(tag: str) -> ClientToolHost:
            tools = ClientToolHost(client.protocol, client_id=client.client_id)

            async def run(_action: dict[str, Any]) -> dict[str, Any]:
                runs.append(tag)
                return await _ok()

            tools.register({"name": "usages"}, run)
            return tools

        a = await client.create_session(provider="echo", uri="echo:/a", tools=tool_host("a"))
        b = await client.create_session(provider="echo", uri="echo:/b", tools=tool_host("b"))
        chat_a = await a.chat()
        await b.chat()  # B's pump is live and watching its own chats
        await host.push(chat_a.uri, {"type": "chat/turnStarted", "turnId": "t1"})
        await host._emit_tool(
            chat_a.uri,
            "t1",
            FakeToolCall(
                "tc1", "usages", contributor={"kind": "client", "clientId": client.client_id}
            ),
        )
        await _settle(lambda: runs)
        await asyncio.sleep(0.2)
        assert runs == ["a"]
        assert len(_dispatched(host, "chat/toolCallComplete")) == 1
    await host.stop()


async def test_a_call_the_tap_never_delivered_is_executed_from_state() -> None:
    """The pump is level-triggered on the mirror, not edge-triggered on the
    events tap. The tap is bounded and drop-oldest, so under load a
    `chat/toolCallReady` can be evicted before a slow reader sees it -- the
    mirror still shows the call running, and the next wake finds it."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)
        runs = 0

        async def counted(_action: dict[str, Any]) -> dict[str, Any]:
            nonlocal runs
            runs += 1
            return await _ok()

        tools.register({"name": "usages"}, counted)
        session = await client.create_session(provider="echo", tools=tools)
        chat = await session.chat()
        # Drive the handover through the mirror alone: these envelopes never
        # reach the events tap, which is exactly what an eviction (or a
        # disconnected window) looks like to the pump.
        for action in (
            {"type": "chat/turnStarted", "turnId": "t1"},
            {
                "type": "chat/toolCallStart",
                "turnId": "t1",
                "toolCallId": "tc1",
                "toolName": "usages",
                "contributor": {"kind": "client", "clientId": client.client_id},
            },
            {
                "type": "chat/toolCallReady",
                "turnId": "t1",
                "toolCallId": "tc1",
                "invocationMessage": "Running usages",
                "confirmed": "not-needed",
            },
        ):
            client.mirror.apply(
                {"channel": chat.uri, "action": action, "serverSeq": host.next_server_seq()}
            )
        await asyncio.sleep(0.1)
        assert runs == 0  # no event has woken the pump yet
        # Any event on this session's channels is the clock.
        await host.push(session.uri, {"type": "session/titleChanged", "title": "T"})
        # The handler returning is not yet its completion being dispatched.
        await _settle(lambda: runs == 1 and bool(_dispatched(host, "chat/toolCallComplete")))
        completion = _dispatched(host, "chat/toolCallComplete")[-1]
        assert completion["turnId"] == "t1"
        assert completion["toolCallId"] == "tc1"
    await host.stop()


async def test_work_already_handed_over_in_the_snapshot_is_picked_up_on_attach() -> None:
    """A snapshot-arm reconnect replays no actions, so a call handed over
    while this client was away produces no event, ever. The designed recovery
    path is `SessionState.inputNeeded`: `toolClientExecution` entries exist
    "so a client that provides the tool can pick up the work without
    subscribing to the owning chat" -- and they are all a fresh snapshot needs
    to carry."""
    host = _session_host(
        inputNeeded=[
            {
                "kind": "toolClientExecution",
                "id": f"{CHAT}#tc1",
                "chat": CHAT,
                "turnId": "t1",
                "toolCall": {
                    "toolCallId": "tc1",
                    "toolName": "usages",
                    "status": "running",
                    "confirmed": "not-needed",
                    "toolInput": "{}",
                    "contributor": {"kind": "client", "clientId": "me"},
                },
            }
        ]
    )
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)
        runs = 0

        async def counted(_action: dict[str, Any]) -> dict[str, Any]:
            nonlocal runs
            runs += 1
            return await _ok()

        tools.register({"name": "usages"}, counted)
        await client.create_session(provider="echo", tools=tools)
        # The handler returning is not yet its completion being dispatched.
        await _settle(lambda: runs == 1 and bool(_dispatched(host, "chat/toolCallComplete")))
        completion = _dispatched(host, "chat/toolCallComplete")[-1]
        assert completion["turnId"] == "t1"
        assert completion["toolCallId"] == "tc1"
    await host.stop()


async def test_a_slow_tool_does_not_block_later_calls() -> None:
    """Each executor runs on its own task. Awaited inline, one hung tool
    stalls the pump's reader while the bounded fan-in tap fills behind it, and
    every later call -- including the auto-denials `ClientToolHost`
    guarantees -- waits on an unrelated tool."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        tools = ClientToolHost(client.protocol, client_id=client.client_id)
        release = asyncio.Event()

        async def slow(_action: dict[str, Any]) -> dict[str, Any]:
            await release.wait()
            return await _ok()

        async def fast(_action: dict[str, Any]) -> dict[str, Any]:
            return await _ok()

        tools.register({"name": "slow"}, slow)
        tools.register({"name": "fast"}, fast)
        session = await client.create_session(provider="echo", tools=tools)
        chat = await session.chat()
        await host.push(chat.uri, {"type": "chat/turnStarted", "turnId": "t1"})
        ours = {"kind": "client", "clientId": client.client_id}
        await host._emit_tool(chat.uri, "t1", FakeToolCall("tc1", "slow", contributor=ours))
        await host._emit_tool(chat.uri, "t1", FakeToolCall("tc2", "fast", contributor=ours))

        def completed(tool_call_id: str) -> list[dict[str, Any]]:
            return [
                c
                for c in _dispatched(host, "chat/toolCallComplete")
                if c["toolCallId"] == tool_call_id
            ]

        await _settle(lambda: completed("tc2"))
        assert completed("tc1") == []  # still running, blocking nothing
        release.set()
        await _settle(lambda: completed("tc1"))
    await host.stop()


async def test_a_fork_needs_no_provider_because_the_source_session_has_one() -> None:
    """`CreateSessionParams.provider` is optional (`provider?: string`): a fork
    inherits it from the source session, so a wrapper requiring it made the
    fork flow unexpressible without lying to the annotation `mypy --strict`
    enforces."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        await client.protocol.create_session(
            "echo:/fork-1", fork={"session": "echo:/source", "turnId": "t1"}
        )
        params = _sent(host, "createSession")[-1]
        assert "provider" not in params
        assert params["fork"] == {"session": "echo:/source", "turnId": "t1"}
    await host.stop()


# ── fetchTurns ───────────────────────────────────────────────────────────────


async def test_fetch_turns_takes_the_chat_channel_not_the_session() -> None:
    """`FetchTurnsParams.channel` is documented "Chat URI" -- the URI from
    `ChatState` / the session's `defaultChat` -- and the reference host
    rejects a session URI with InvalidParams ("... is not a chat channel").
    The wrapper's old signature named the parameter `session`, teaching every
    caller exactly the argument that cannot work."""
    host = _session_host()
    host.on("fetchTurns", lambda _p: {})
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        await client.protocol.fetch_turns(chat.uri, cursor="page-2")
        params = _sent(host, "fetchTurns")[-1]
        assert params["channel"] == chat.uri
        assert params["cursor"] == "page-2"
    await host.stop()


# ── what the handshake advertised ────────────────────────────────────────────


async def test_the_terminal_command_prefix_reaches_a_connect_caller() -> None:
    """The host advertises `!` and only a client can implement the shorthand.
    The runtime kept `completionTriggerCharacters`, the directly analogous
    advertisement, and dropped this one on the floor."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        assert client.terminal_command_prefix == "!"
        assert list(client.completion_trigger_characters) == ["@", "/"]
        assert client.default_directory == "file:///work"
    await host.stop()


async def test_a_host_with_no_prefix_reports_none_rather_than_an_empty_string() -> None:
    """ "Absence means the host does not support command prefixes", and `""` is
    the same answer -- a caller testing `if prefix:` and one testing
    `if prefix is not None:` must not disagree."""
    host = echo_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        assert client.terminal_command_prefix is None
    await host.stop()
