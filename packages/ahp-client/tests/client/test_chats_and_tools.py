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
from agent_host_protocol.conformance.corpus import CORPUS_ROOT

from agent_host_client.api import connect
from agent_host_client.client.errors import AhpClientError
from agent_host_client.serve import ClientToolHost
from agent_host_client.testing import FakeHost, FakeToolCall, echo_host

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
    host = _session_host(capabilities={"multipleChats": {"fork": True}})
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        await session.create_chat(
            initial_message="pick up from here",
            source={"kind": "fork", "chat": CHAT, "turnId": "t1"},
            working_directories=["file:///work"],
        )
        params = _sent(host, "createChat")[-1]
        assert params["initialMessage"] == {"text": "pick up from here"}
        assert params["source"]["kind"] == "fork"
        assert params["workingDirectories"] == ["file:///work"]
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
        await host._emit_tool(
            chat.uri, "t1", FakeToolCall("tc1", "usages", contributor={"kind": "client"})
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
        call = FakeToolCall("tc1", "usages", contributor={"kind": "client"})
        await host._emit_tool(chat.uri, "t1", call)
        await _settle(lambda: runs == 1)
        await host._emit_tool(chat.uri, "t1", call)
        await asyncio.sleep(0.1)
        assert runs == 1
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
