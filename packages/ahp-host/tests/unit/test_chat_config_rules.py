"""Chat config does not inherit from the session (upstream `c301d494`, H1).

`ChatConfigState.values` holds the chat's own value for each `chatMutable`
property; a missing key takes the schema default, never the session's value; a
chat's values change only through `chat/configChanged` on that chat. The host
sets them when it creates the chat, on every chat, whenever the session has a
config. Seeding from the session's *current* value keeps what a new chat did
before the rule, and materialising once on restore keeps every existing chat.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.chat_config import (
    materialise_chat_config,
    seed_chat_config,
)
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider, EchoSession

from .hosting import Wire, connect, open_session, run_turn, shut, state

pytestmark = pytest.mark.anyio

_CHATS: dict[str, Any] = {"multipleChats": {"fork": True, "sideChat": True}}

_SESSION = {
    "config": {
        "schema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "default": "You said:", "chatMutable": True},
                "style": {"type": "string", "default": "plain"},
                "mode": {"type": "string", "chatMutable": True},
            },
        },
        "values": {"style": "shout", "mode": "ask"},
    }
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# -- the helper ---------------------------------------------------------------


def test_a_session_without_config_gives_chats_none() -> None:
    assert seed_chat_config({}, None) is None
    assert materialise_chat_config({}, {}) is None


def test_a_new_chat_takes_the_sessions_value_else_the_default_for_chat_mutable_keys() -> None:
    seeded = seed_chat_config(_SESSION)
    # `style` is session-wide so never copied; `prefix` has no session value so
    # takes its default; `mode` takes the session's value.
    assert seeded == {"values": {"prefix": "You said:", "mode": "ask"}}


def test_a_fork_or_side_chat_copies_its_sources_values_not_the_sessions() -> None:
    source = {"config": {"values": {"prefix": "Mine:", "mode": "plan"}}}
    seeded = seed_chat_config(_SESSION, source)
    assert seeded == {"values": {"prefix": "Mine:", "mode": "plan"}}
    assert seeded is not source["config"]


def test_a_property_with_neither_session_value_nor_default_is_left_out() -> None:
    session = {
        "config": {
            "schema": {"properties": {"x": {"chatMutable": True}}},
            "values": {},
        }
    }
    assert seed_chat_config(session) == {"values": {}}


def test_materialise_prefers_the_chat_then_the_session_then_the_default() -> None:
    chat = {"config": {"values": {"prefix": "Mine:"}}}
    assert materialise_chat_config(chat, _SESSION) == {"values": {"prefix": "Mine:", "mode": "ask"}}
    assert materialise_chat_config({}, _SESSION) == {
        "values": {"prefix": "You said:", "mode": "ask"}
    }


def test_materialise_ignores_keys_that_are_not_chat_mutable() -> None:
    chat = {"config": {"values": {"style": "plain"}}}
    assert "style" not in materialise_chat_config(chat, _SESSION)["values"]  # type: ignore[index]


def test_a_chat_with_its_own_schema_never_uses_the_sessions_config() -> None:
    chat = {
        "config": {
            "schema": {"properties": {"depth": {"chatMutable": True, "default": 3}}},
            "values": {},
        }
    }
    assert materialise_chat_config(chat, _SESSION) == {
        "schema": chat["config"]["schema"],
        "values": {"depth": 3},
    }


# -- through a real host ------------------------------------------------------


def _provider() -> EchoProvider:
    return EchoProvider(configurable=True, capabilities=_CHATS)


async def _set_session_prefix(wire: Wire, uri: str, prefix: str) -> None:
    seq = await wire.dispatch(uri, {"type": "session/configChanged", "config": {"prefix": prefix}})
    echo = await wire.echoed(uri, seq)
    assert "rejectionReason" not in echo, echo


async def _new_chat(wire: Wire, session: str, chat: str, **extra: Any) -> str:
    response = await wire.request("createChat", {"channel": session, "chat": chat, **extra})
    assert "error" not in response, response
    await wire.request("subscribe", {"channel": chat})
    return chat


def _values(host: Host, chat: str) -> Any:
    return state(host, chat).get("config")


async def _reply(wire: Wire, chat: str, turn: str) -> str:
    await run_turn(wire, chat, turn)
    turns = state(wire.host, chat)["turns"]
    last = next(t for t in turns if t["id"] == turn)
    return "".join(
        p.get("content", "") for p in last["responseParts"] if p.get("kind") == "markdown"
    )


async def test_every_chat_gets_a_config_when_the_session_has_one() -> None:
    host = Host(_provider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/cc-1")
        other = await _new_chat(wire, "echo:/cc-1", "ahp-chat:/cc-1b")
        assert _values(host, default) == {"values": {"prefix": "You said:"}}
        assert _values(host, other) == {"values": {"prefix": "You said:"}}
    finally:
        await shut(host, wire, serving)


async def test_a_session_without_config_leaves_chats_without_one() -> None:
    host = Host(EchoProvider(capabilities=_CHATS), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/cc-2")
        other = await _new_chat(wire, "echo:/cc-2", "ahp-chat:/cc-2b")
        assert "config" not in state(host, default)
        assert "config" not in state(host, other)
    finally:
        await shut(host, wire, serving)


async def test_a_session_change_moves_no_existing_chat_and_seeds_new_ones() -> None:
    host = Host(_provider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/cc-3")
        await _set_session_prefix(wire, "echo:/cc-3", "Loud:")
        # The existing chat does NOT follow, in state or in what the agent does.
        assert _values(host, default) == {"values": {"prefix": "You said:"}}
        assert (await _reply(wire, default, "t1")).startswith("You said:")
        # A chat created now starts from the session's current value.
        fresh = await _new_chat(wire, "echo:/cc-3", "ahp-chat:/cc-3b")
        assert _values(host, fresh) == {"values": {"prefix": "Loud:"}}
        assert (await _reply(wire, fresh, "t2")).startswith("Loud:")
    finally:
        await shut(host, wire, serving)


async def test_a_chat_value_changes_only_through_chat_config_changed() -> None:
    host = Host(_provider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/cc-4")
        seq = await wire.dispatch(
            default, {"type": "chat/configChanged", "config": {"prefix": "A:"}}
        )
        assert "rejectionReason" not in await wire.echoed(default, seq)
        await _set_session_prefix(wire, "echo:/cc-4", "B:")
        assert _values(host, default) == {"values": {"prefix": "A:"}}
        assert (await _reply(wire, default, "t1")).startswith("A:")
        # `replace` with the key left out is the DEFAULT, not the session's "B:".
        seq = await wire.dispatch(
            default, {"type": "chat/configChanged", "config": {}, "replace": True}
        )
        assert "rejectionReason" not in await wire.echoed(default, seq)
        assert (await _reply(wire, default, "t2")).startswith("You said:")
    finally:
        await shut(host, wire, serving)


async def test_a_fork_and_a_side_chat_copy_their_sources_values() -> None:
    host = Host(_provider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/cc-5")
        seq = await wire.dispatch(
            default, {"type": "chat/configChanged", "config": {"prefix": "S:"}}
        )
        await wire.echoed(default, seq)
        await run_turn(wire, default, "t1")
        await _set_session_prefix(wire, "echo:/cc-5", "Later:")
        fork = await _new_chat(
            wire,
            "echo:/cc-5",
            "ahp-chat:/cc-5f",
            source={"kind": "fork", "chat": default, "turnId": "t1"},
        )
        side = await _new_chat(
            wire,
            "echo:/cc-5",
            "ahp-chat:/cc-5s",
            source={"kind": "sideChat", "chat": default, "turnId": "t1"},
        )
        assert _values(host, fork) == {"values": {"prefix": "S:"}}
        assert _values(host, side) == {"values": {"prefix": "S:"}}
    finally:
        await shut(host, wire, serving)


async def test_a_forked_session_seeds_its_default_chat_from_the_source_chat() -> None:
    host = Host(_provider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/cc-6")
        seq = await wire.dispatch(
            default, {"type": "chat/configChanged", "config": {"prefix": "S:"}}
        )
        await wire.echoed(default, seq)
        await run_turn(wire, default, "t1")
        response = await wire.request(
            "createSession",
            {
                "channel": "echo:/cc-6f",
                "provider": "echo",
                "fork": {"session": "echo:/cc-6", "turnId": "t1"},
            },
        )
        assert "error" not in response, response
        assert await wire.until(lambda: state(host, "echo:/cc-6f").get("lifecycle") == "ready")
        forked = state(host, "echo:/cc-6f")["defaultChat"]
        assert _values(host, forked) == {"values": {"prefix": "S:"}}
    finally:
        await shut(host, wire, serving)


async def test_a_moved_chat_keeps_its_values_across_sessions() -> None:
    host = Host(_provider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/cc-7a")
        await open_session(wire, "echo:/cc-7b")
        chat = await _new_chat(wire, "echo:/cc-7a", "ahp-chat:/cc-7m")
        seq = await wire.dispatch(chat, {"type": "chat/configChanged", "config": {"prefix": "M:"}})
        await wire.echoed(chat, seq)
        await _set_session_prefix(wire, "echo:/cc-7b", "Target:")
        moved = await wire.request(
            "moveChat",
            {"channel": chat, "destination": {"kind": "session", "session": "echo:/cc-7b"}},
        )
        assert "error" not in moved, moved
        assert _values(host, chat) == {"values": {"prefix": "M:"}}
        new = await wire.request(
            "moveChat", {"channel": chat, "destination": {"kind": "newSession"}}
        )
        assert "error" not in new, new
        assert _values(host, chat) == {"values": {"prefix": "M:"}}
    finally:
        await shut(host, wire, serving)


async def test_the_agent_is_told_every_chats_complete_values_when_it_attaches() -> None:
    host = Host(_provider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/cc-8")
        other = await _new_chat(wire, "echo:/cc-8", "ahp-chat:/cc-8b")
        session = host._sessions["echo:/cc-8"]
        fresh = EchoSession(session.agent_session.context)  # type: ignore[union-attr]
        await host._attach_agent(session, fresh)
        assert fresh.chat_config == {
            default: {"prefix": "You said:"},
            other: {"prefix": "You said:"},
        }
    finally:
        await shut(host, wire, serving)


async def test_a_new_chat_is_told_its_values_too() -> None:
    host = Host(_provider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/cc-9")
        await _set_session_prefix(wire, "echo:/cc-9", "Loud:")
        other = await _new_chat(wire, "echo:/cc-9", "ahp-chat:/cc-9b")
        agent = host._sessions["echo:/cc-9"].agent_session
        assert isinstance(agent, EchoSession)
        assert agent.chat_config[other] == {"prefix": "Loud:"}
    finally:
        await shut(host, wire, serving)


async def test_a_non_chat_mutable_key_is_still_rejected() -> None:
    host = Host(_provider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, "echo:/cc-10")
        seq = await wire.dispatch(default, {"type": "chat/configChanged", "config": {"style": "x"}})
        echo = await wire.echoed(default, seq)
        assert echo["rejectionReason"] == "'style' cannot be set per chat"
    finally:
        await shut(host, wire, serving)


# -- restore ------------------------------------------------------------------


def _rewrite_as_before_the_rule(directory: Path, chats: dict[str, Any]) -> None:
    """Edit the stored file as an older host would have written it."""
    [path] = list(directory.glob("*.json"))
    payload = json.loads(path.read_text())
    payload.pop("chatConfigMaterialised", None)
    for chat, config in chats.items():
        if config is None:
            payload["channels"][chat].pop("config", None)
        else:
            payload["channels"][chat]["config"] = config
    path.write_text(json.dumps(payload))


async def test_restore_materialises_the_overlay_rule_once(tmp_path: Path) -> None:
    first = Host(_provider(), LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path))
    wire, serving = await connect(first)
    uri = "echo:/cc-11"
    try:
        default = await open_session(wire, uri)
        other = await _new_chat(wire, uri, "ahp-chat:/cc-11b")
        await _set_session_prefix(wire, uri, "Session:")
    finally:
        await shut(first, wire, serving)
    # Before the rule the default chat had no config (it followed the session)
    # and another chat had set only its own prefix.
    _rewrite_as_before_the_rule(tmp_path, {default: None, other: {"values": {"prefix": "Mine:"}}})

    second = Host(_provider(), LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path))
    assert await second.restore() == 1
    wire, serving = await connect(second)
    try:
        await wire.request("subscribe", {"channel": uri})
        # Effective behaviour is unchanged: the default chat was on "Session:".
        assert _values(second, default) == {"values": {"prefix": "Session:"}}
        assert _values(second, other) == {"values": {"prefix": "Mine:"}}
        await _set_session_prefix(wire, uri, "Later:")
        assert _values(second, default) == {"values": {"prefix": "Session:"}}
    finally:
        await shut(second, wire, serving)

    # Persisted: a third start does not materialise again, so a value a client
    # cleared on purpose (missing key = the default) is not refilled from the session.
    third = Host(_provider(), LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path))
    assert await third.restore() == 1
    try:
        assert _values(third, default) == {"values": {"prefix": "Session:"}}
        assert state(third, uri)["config"]["values"]["prefix"] == "Later:"
    finally:
        await third.aclose()


async def test_a_value_cleared_on_purpose_is_not_refilled_on_restore(tmp_path: Path) -> None:
    first = Host(_provider(), LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path))
    wire, serving = await connect(first)
    uri = "echo:/cc-12"
    try:
        default = await open_session(wire, uri)
        await _set_session_prefix(wire, uri, "Session:")
        seq = await wire.dispatch(
            default, {"type": "chat/configChanged", "config": {}, "replace": True}
        )
        await wire.echoed(default, seq)
        assert _values(first, default) == {"values": {}}
    finally:
        await shut(first, wire, serving)

    second = Host(_provider(), LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path))
    assert await second.restore() == 1
    try:
        assert _values(second, default) == {"values": {}}
    finally:
        await second.aclose()


def test_the_materialised_flag_round_trips_and_defaults_to_false() -> None:
    from ahp_host.core.store import StoredSession

    stored = StoredSession(uri="echo:/s", provider="echo", created_at="t", channels={})
    assert stored.chat_config_materialised is False
    assert "chatConfigMaterialised" not in stored.to_json()
    flagged = StoredSession(
        uri="echo:/s", provider="echo", created_at="t", channels={}, chat_config_materialised=True
    )
    assert StoredSession.from_json(flagged.to_json()).chat_config_materialised is True
    with pytest.raises(ValueError, match="chatConfigMaterialised"):
        StoredSession.from_json({**flagged.to_json(), "chatConfigMaterialised": "yes"})
