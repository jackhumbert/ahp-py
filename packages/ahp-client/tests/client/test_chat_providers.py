"""Per-chat agent, reasoning timing, `resultMetadata` and the chat config rules
(upstream `c301d494`).

`CreateChatParams.provider` is a MUST NOT without `InitializeResult.
multipleChatProviders`, and the capability checks for a request read the
*chat's* agent. Both are presence flags: `{}` advertises support, so every test
here that matters uses `{}`.
"""

from __future__ import annotations

from typing import Any

import pytest

from ahp_client import Reasoning, ToolCallCompleted, ToolCallResultReview, event_for
from ahp_client.api import connect
from ahp_client.api.config import effective_config
from ahp_client.client.errors import AhpClientError
from ahp_client.testing import FakeHost, echo_host

CHAT = "ahp-chat://c/s"
OTHER = "ahp-chat://c/other"

_AGENTS = [
    {"provider": "echo", "displayName": "Echo", "capabilities": {"multipleChats": {}}},
    {
        "provider": "second",
        "displayName": "Second",
        "capabilities": {"multipleChats": {"fork": True}},
    },
    {"provider": "single", "displayName": "Single", "capabilities": {}},
]


def _host(
    *,
    providers: bool = True,
    chats: list[dict[str, Any]] | None = None,
    session_config: dict[str, Any] | None = None,
    chat_states: dict[str, dict[str, Any]] | None = None,
) -> FakeHost:
    host = FakeHost(agents=_AGENTS)
    state: dict[str, Any] = {
        "lifecycle": "ready",
        "defaultChat": CHAT,
        "interactivity": "full",
        "chats": chats or [],
    }
    if session_config is not None:
        state["config"] = session_config

    def initialize(params: dict[str, Any]) -> dict[str, Any]:
        snapshots = [
            {"resource": uri, "state": host.root_state, "fromSeq": host._server_seq}
            for uri in params.get("initialSubscriptions") or []
            if uri == "ahp-root://"
        ]
        result: dict[str, Any] = {
            "protocolVersion": host.protocol_version,
            "serverSeq": host._server_seq,
            "snapshots": snapshots,
        }
        if providers:
            result["multipleChatProviders"] = {}
        return result

    def subscribe(params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        if channel.startswith("ahp-chat:"):
            body: Any = {"turns": [], **(chat_states or {}).get(channel, {})}
        elif channel == "ahp-root://":
            body = host.root_state
        else:
            body = state
        return {"snapshot": {"resource": channel, "state": body, "fromSeq": host._server_seq}}

    host.on("initialize", initialize)
    host.on("ping", lambda _p: {})
    host.on("listSessions", lambda _p: {"items": []})
    host.on("subscribe", subscribe)
    host.on("createSession", lambda _p: {})
    host.on("createChat", lambda _p: {})
    return host


def _sent(host: FakeHost, method: str) -> list[dict[str, Any]]:
    return [m["params"] for m in host.received if m.get("method") == method]


# -- the handshake ------------------------------------------------------------


async def test_the_handshake_capability_is_exposed_as_a_presence_flag() -> None:
    for advertised in (True, False):
        host = _host(providers=advertised)
        await host.start()
        async with connect(transport=host.transport()) as client:
            assert client.multiple_chat_providers is advertised
        await host.stop()


# -- create_chat(provider=) ---------------------------------------------------


async def test_provider_is_sent_only_when_given() -> None:
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        await session.create_chat()
        await session.create_chat(provider="second")
        plain, foreign = _sent(host, "createChat")
        assert "provider" not in plain
        assert foreign["provider"] == "second"
    await host.stop()


async def test_provider_is_refused_locally_without_the_capability() -> None:
    host = _host(providers=False)
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        with pytest.raises(AhpClientError, match="multipleChatProviders"):
            await session.create_chat(provider="second")
        assert _sent(host, "createChat") == []
        # Without `provider` nothing changes.
        await session.create_chat()
        assert len(_sent(host, "createChat")) == 1
    await host.stop()


async def test_the_capability_check_is_presence_not_truthiness() -> None:
    """`multipleChatProviders: {}` is falsy in Python and is the capability."""
    host = _host(providers=True)
    await host.start()
    async with connect(transport=host.transport()) as client:
        assert client._runtime.multiple_chat_providers is True
        session = await client.create_session(provider="echo")
        await session.create_chat(provider="second")
    await host.stop()


async def test_multiple_chats_is_read_from_the_chosen_provider() -> None:
    """The session's agent supports multi-chat; the requested one does not."""
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        with pytest.raises(AhpClientError, match=r"'single'.*multipleChats"):
            await session.create_chat(provider="single")
        assert _sent(host, "createChat") == []
    await host.stop()


async def test_a_provider_the_root_does_not_list_is_refused() -> None:
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        with pytest.raises(AhpClientError, match="nowhere"):
            await session.create_chat(provider="nowhere")
        assert _sent(host, "createChat") == []
    await host.stop()


async def test_fork_and_side_chat_gates_read_the_chosen_provider() -> None:
    """`echo` has no fork; `second` does. The provider decides, not the session."""
    host = _host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        source = {"kind": "fork", "chat": CHAT, "turnId": "t1"}
        with pytest.raises(AhpClientError, match="fork"):
            await session.create_chat(source=source)
        await session.create_chat(source=source, provider="second")
        assert _sent(host, "createChat")[-1]["provider"] == "second"
        with pytest.raises(AhpClientError, match="sideChat"):
            await session.create_chat(source={"kind": "sideChat", "chat": CHAT}, provider="second")
    await host.stop()


async def test_a_fork_of_a_foreign_chat_reads_the_source_chats_agent() -> None:
    """With `provider` left out a fork means the source chat's agent."""
    host = _host(
        chats=[{"resource": OTHER, "title": "x", "provider": "second"}],
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        await session.create_chat(source={"kind": "fork", "chat": OTHER, "turnId": "t1"})
        assert "provider" not in _sent(host, "createChat")[-1]
    await host.stop()


async def test_the_raw_command_sends_provider_only_when_given_and_gates_after_initialize() -> None:
    host = _host(providers=False)
    await host.start()
    async with connect(transport=host.transport()) as client:
        raw = client.protocol
        await raw.create_chat("ahp-session:/s", "ahp-chat:/a")
        assert "provider" not in _sent(host, "createChat")[-1]
        with pytest.raises(AhpClientError, match="multipleChatProviders"):
            await raw.create_chat("ahp-session:/s", "ahp-chat:/b", provider="second")
        assert len(_sent(host, "createChat")) == 1
    await host.stop()
    host = _host(providers=True)
    await host.start()
    async with connect(transport=host.transport()) as client:
        await client.protocol.create_chat("ahp-session:/s", "ahp-chat:/b", provider="second")
        assert _sent(host, "createChat")[-1]["provider"] == "second"
    await host.stop()


# -- views --------------------------------------------------------------------


async def test_chat_provider_is_the_effective_agent() -> None:
    host = _host(
        chats=[{"resource": OTHER, "title": "x", "provider": "second"}],
        chat_states={OTHER: {"provider": "second"}},
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        assert (await session.chat()).provider == "echo"
        assert (await session.open_chat(OTHER)).provider == "second"
        assert session.chats()[0]["provider"] == "second"
    await host.stop()


# -- events -------------------------------------------------------------------


def _envelope(action: dict[str, object]) -> dict[str, object]:
    return {"channel": CHAT, "action": action, "serverSeq": 1}


def test_chat_moved_is_a_typed_event() -> None:
    from ahp_client import ChatMoved

    schema = {"type": "object", "properties": {}}
    event = event_for(
        _envelope(
            {"type": "chat/moved", "session": "ahp-session:/s2", "provider": "p", "schema": schema}
        )
    )
    assert isinstance(event, ChatMoved)
    assert (event.session, event.provider, event.schema) == ("ahp-session:/s2", "p", schema)
    bare = event_for(_envelope({"type": "chat/moved", "session": "ahp-session:/s2"}))
    assert isinstance(bare, ChatMoved)
    assert bare.provider is None
    assert bare.schema is None


def test_reasoning_carries_its_timing() -> None:
    plain = event_for(
        _envelope({"type": "chat/reasoning", "turnId": "t", "partId": "r", "content": "a"})
    )
    assert isinstance(plain, Reasoning)
    assert plain.started_at is None
    assert plain.duration is None
    timed = event_for(
        _envelope(
            {
                "type": "chat/reasoning",
                "turnId": "t",
                "partId": "r",
                "content": "",
                "startedAt": "2026-10-19T12:00:00.000Z",
                "duration": 0,
            }
        )
    )
    assert isinstance(timed, Reasoning)
    assert timed.started_at == "2026-10-19T12:00:00.000Z"
    # Zero is a recorded duration, not "unknown".
    assert timed.duration == 0


def test_tool_results_carry_result_metadata() -> None:
    meta = {"phase": "server-result", "nullable": None}
    result = {"success": True, "pastTenseMessage": "done", "resultMetadata": meta}
    base = {"turnId": "t", "toolCallId": "c", "result": result}
    done = event_for(_envelope({"type": "chat/toolCallComplete", **base}))
    assert isinstance(done, ToolCallCompleted)
    assert done.result_metadata == meta
    review = event_for(
        _envelope({"type": "chat/toolCallComplete", **base, "requiresResultConfirmation": True})
    )
    assert isinstance(review, ToolCallResultReview)
    assert review.result_metadata == meta
    bare = event_for(
        _envelope(
            {
                "type": "chat/toolCallComplete",
                "turnId": "t",
                "toolCallId": "c",
                "result": {"success": True, "pastTenseMessage": "done"},
            }
        )
    )
    assert isinstance(bare, ToolCallCompleted)
    assert bare.result_metadata is None


# -- effective config ---------------------------------------------------------

_SESSION_CFG: dict[str, Any] = {
    "schema": {
        "type": "object",
        "properties": {
            "approvals": {"type": "string", "default": "ask", "chatMutable": True},
            "effort": {"type": "string", "chatMutable": True},
            "model": {"type": "string", "default": "m0"},
        },
    },
    "values": {"approvals": "auto", "effort": "high", "model": "m1"},
}


def test_effective_config_uses_the_chats_values_for_chat_mutable_keys() -> None:
    chat = {"config": {"values": {"approvals": "never"}}}
    assert effective_config({"config": _SESSION_CFG}, chat) == {
        "approvals": "never",
        # `effort` has no chat value and no default: the session's "high" must
        # not leak in.
        "model": "m1",
    }


def test_a_missing_key_takes_the_schema_default_not_the_sessions_value() -> None:
    chat: dict[str, Any] = {"config": {"values": {}}}
    assert effective_config({"config": _SESSION_CFG}, chat) == {"approvals": "ask", "model": "m1"}


def test_a_chat_without_config_still_does_not_inherit_chat_mutable_values() -> None:
    assert effective_config({"config": _SESSION_CFG}, {}) == {"approvals": "ask", "model": "m1"}


def test_a_chat_with_its_own_schema_ignores_the_sessions_config() -> None:
    chat = {
        "config": {
            "schema": {
                "type": "object",
                "properties": {
                    "flag": {"type": "boolean", "default": False, "chatMutable": True},
                    "other": {"type": "string", "default": "d", "chatMutable": True},
                },
            },
            "values": {"flag": True},
        }
    }
    assert effective_config({"config": _SESSION_CFG}, chat) == {"flag": True, "other": "d"}


def test_a_session_change_does_not_move_a_chat_that_has_values() -> None:
    chat = {"config": {"values": {"approvals": "never"}}}
    loosened = {
        "config": {**_SESSION_CFG, "values": {**_SESSION_CFG["values"], "approvals": "always"}}
    }
    assert effective_config(loosened, chat)["approvals"] == "never"


def test_no_schema_anywhere_returns_the_chats_values() -> None:
    assert effective_config({}, {"config": {"values": {"a": 1}}}) == {"a": 1}
    assert effective_config({}, {}) == {}


async def test_chat_effective_config_reads_the_mirror() -> None:
    host = _host(
        session_config=_SESSION_CFG,
        chat_states={CHAT: {"config": {"values": {"effort": "low"}}}},
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        assert chat.effective_config() == {"approvals": "ask", "effort": "low", "model": "m1"}
    await host.stop()


async def test_the_fake_host_advertises_the_capability_as_a_presence_object() -> None:
    for advertised in (True, False):
        host = echo_host(multiple_chat_providers=advertised)
        await host.start()
        async with connect(transport=host.transport()) as client:
            assert client.multiple_chat_providers is advertised
        await host.stop()
