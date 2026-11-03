"""Per-chat agents (AHP 1.1.0, upstream `c301d494`): the capability and `createChat.provider`.

`Host(multiple_chat_providers=True)` advertises `InitializeResult.multipleChatProviders`
(an opt-in, because it changes what a session persists). Without it `createChat` with a
`provider` is `InvalidParams`.
"""

from __future__ import annotations

from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider

from .hosting import connect, open_session, run_turn, shut, state

pytestmark = pytest.mark.anyio

_CHATS: dict[str, Any] = {"multipleChats": {"fork": True, "sideChat": True}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _providers() -> list[EchoProvider]:
    return [
        EchoProvider(capabilities=_CHATS),
        EchoProvider(provider_id="echo2", display_name="Echo 2", capabilities=_CHATS),
    ]


async def test_the_capability_is_absent_unless_the_embedder_opts_in() -> None:
    host = Host(_providers(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        assert "multipleChatProviders" not in wire.initialized
    finally:
        await shut(host, wire, serving)


async def test_the_capability_is_a_presence_only_object_when_enabled() -> None:
    host = Host(_providers(), LoopbackSingleUserPolicy(), multiple_chat_providers=True)
    wire, serving = await connect(host)
    try:
        assert wire.initialized["multipleChatProviders"] == {}
    finally:
        await shut(host, wire, serving)


async def test_create_chat_with_a_provider_is_invalid_params_without_the_capability() -> None:
    host = Host(_providers(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/mp-1")
        response = await wire.request(
            "createChat",
            {"channel": "echo:/mp-1", "chat": "ahp-chat:/mp-1b", "provider": "echo2"},
        )
        assert response["error"]["code"] == -32602, response
        assert "multipleChatProviders" in response["error"]["message"]
        assert not host.sequencer.has_channel("ahp-chat:/mp-1b")
    finally:
        await shut(host, wire, serving)


# -- createChat with a provider (M2) ------------------------------------------


def _pair(*, second: dict[str, Any] | None = _CHATS, configurable: bool = True) -> Host:
    """Two echo agents; the second can be given other capabilities (or none)."""
    return Host(
        [
            EchoProvider(capabilities=_CHATS, configurable=True),
            EchoProvider(
                provider_id="echo2",
                display_name="Echo 2",
                capabilities=second,
                configurable=configurable,
            ),
        ],
        LoopbackSingleUserPolicy(),
        multiple_chat_providers=True,
    )


async def _create(wire: Any, session: str, chat: str, **extra: Any) -> dict[str, Any]:
    response: dict[str, Any] = await wire.request(
        "createChat", {"channel": session, "chat": chat, **extra}
    )
    return response


def _chat_state(host: Host, chat: str) -> dict[str, Any]:
    return state(host, chat)


async def test_a_chat_on_another_agent_reports_its_provider_and_config() -> None:
    host = _pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/mp-2")
        response = await _create(wire, "echo:/mp-2", "ahp-chat:/mp-2b", provider="echo2")
        assert "error" not in response, response
        chat = _chat_state(host, "ahp-chat:/mp-2b")
        assert chat["provider"] == "echo2"
        # The foreign agent's own schema, chatMutable properties only, at their defaults.
        config = chat["config"]
        assert list(config["schema"]["properties"]) == ["prefix"]
        assert config["schema"]["properties"]["prefix"]["chatMutable"] is True
        assert config["values"] == {"prefix": "You said:"}
        # Reported in the catalogue and the compact summary too.
        entry = next(
            c for c in state(host, "echo:/mp-2")["chats"] if c["resource"] == "ahp-chat:/mp-2b"
        )
        assert entry["provider"] == "echo2"
        added = [
            a
            for a in wire.actions("echo:/mp-2", "session/chatAdded")
            if a["summary"]["resource"] == "ahp-chat:/mp-2b"
        ]
        assert added[-1]["summary"]["provider"] == "echo2"
        summary = host._full_summary(host._sessions["echo:/mp-2"])
        compact = next(c for c in summary["chats"] if c["resource"] == "ahp-chat:/mp-2b")
        assert compact["provider"] == "echo2"
        # The session's own default chat reports none.
        assert "provider" not in _chat_state(host, state(host, "echo:/mp-2")["defaultChat"])
    finally:
        await shut(host, wire, serving)


async def test_a_chat_on_the_sessions_own_agent_reports_no_provider() -> None:
    host = _pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/mp-3")
        response = await _create(wire, "echo:/mp-3", "ahp-chat:/mp-3b", provider="echo")
        assert "error" not in response, response
        chat = _chat_state(host, "ahp-chat:/mp-3b")
        assert "provider" not in chat
        assert "schema" not in chat["config"]
        entry = next(
            c for c in state(host, "echo:/mp-3")["chats"] if c["resource"] == "ahp-chat:/mp-3b"
        )
        assert "provider" not in entry
    finally:
        await shut(host, wire, serving)


async def test_a_foreign_agent_without_config_leaves_the_chat_without_one() -> None:
    host = _pair(configurable=False)
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/mp-4")
        await _create(wire, "echo:/mp-4", "ahp-chat:/mp-4b", provider="echo2")
        chat = _chat_state(host, "ahp-chat:/mp-4b")
        assert chat["provider"] == "echo2"
        assert "config" not in chat
        # The session's config does not govern it: nothing can be set on this chat.
        await wire.request("subscribe", {"channel": "ahp-chat:/mp-4b"})
        seq = await wire.dispatch(
            "ahp-chat:/mp-4b", {"type": "chat/configChanged", "config": {"prefix": "x"}}
        )
        echo = await wire.echoed("ahp-chat:/mp-4b", seq)
        assert "rejectionReason" in echo
    finally:
        await shut(host, wire, serving)


async def test_a_foreign_chat_is_validated_against_its_own_schema() -> None:
    host = _pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/mp-5")
        await _create(wire, "echo:/mp-5", "ahp-chat:/mp-5b", provider="echo2")
        await wire.request("subscribe", {"channel": "ahp-chat:/mp-5b"})
        ok = await wire.dispatch(
            "ahp-chat:/mp-5b", {"type": "chat/configChanged", "config": {"prefix": "Hi:"}}
        )
        assert "rejectionReason" not in await wire.echoed("ahp-chat:/mp-5b", ok)
        bad = await wire.dispatch(
            "ahp-chat:/mp-5b", {"type": "chat/configChanged", "config": {"style": "shout"}}
        )
        assert "rejectionReason" in await wire.echoed("ahp-chat:/mp-5b", bad)
        assert _chat_state(host, "ahp-chat:/mp-5b")["config"]["values"] == {"prefix": "Hi:"}
    finally:
        await shut(host, wire, serving)


@pytest.mark.parametrize(
    ("kwargs", "second", "needle"),
    [
        ({"provider": "nope"}, _CHATS, "nope"),
        ({"provider": "echo2"}, None, "multipleChats"),
        ({"provider": 7}, _CHATS, "provider"),
    ],
)
async def test_a_provider_the_host_cannot_use_is_invalid_params(
    kwargs: dict[str, Any], second: dict[str, Any] | None, needle: str
) -> None:
    host = _pair(second=second)
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/mp-6")
        response = await _create(wire, "echo:/mp-6", "ahp-chat:/mp-6b", **kwargs)
        assert response["error"]["code"] == -32602, response
        assert needle in response["error"]["message"]
        assert not host.sequencer.has_channel("ahp-chat:/mp-6b")
        assert "ahp-chat:/mp-6b" not in host._sessions["echo:/mp-6"].chat_uris
    finally:
        await shut(host, wire, serving)


async def test_a_fork_or_side_chat_must_use_its_sources_agent() -> None:
    host = _pair()
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/mp-7")
        await run_turn(wire, default, "t1")
        for kind, chat in (("fork", "ahp-chat:/mp-7f"), ("sideChat", "ahp-chat:/mp-7s")):
            response = await _create(
                wire,
                "echo:/mp-7",
                chat,
                provider="echo2",
                source={"kind": kind, "chat": default, "turnId": "t1"},
            )
            assert response["error"]["code"] == -32602, response
            assert not host.sequencer.has_channel(chat)
        # Absent means the source's agent; naming it is fine too.
        ok = await _create(
            wire,
            "echo:/mp-7",
            "ahp-chat:/mp-7g",
            provider="echo",
            source={"kind": "fork", "chat": default, "turnId": "t1"},
        )
        assert "error" not in ok, ok
    finally:
        await shut(host, wire, serving)


async def test_capability_checks_use_the_chats_agent() -> None:
    """The session's agent forks; the requested one does not. A fork is the source's, so
    the check is the session's -- but a plain chat needs the requested agent's own flag."""
    host = _pair(second={"multipleChats": {}})
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/mp-8")
        ok = await _create(wire, "echo:/mp-8", "ahp-chat:/mp-8b", provider="echo2")
        assert "error" not in ok, ok
        # Working directories need the chat's agent to advertise them.
        refused = await _create(
            wire,
            "echo:/mp-8",
            "ahp-chat:/mp-8c",
            provider="echo2",
            workingDirectories=["file:///work"],
        )
        assert refused["error"]["code"] == -32602, refused
        assert "multipleWorkingDirectories" in refused["error"]["message"]
    finally:
        await shut(host, wire, serving)
