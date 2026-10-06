"""Phase-two surfaces against `fake_agent.py`: an ACP session per chat, forks,
per-chat cancel and close, the agent's permission options, `fileEdit` results,
and `AgentInfo` that follows what the agent reports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_host import AhpError, Host, LoopbackSingleUserPolicy
from ahp_host.provider.base import (
    CancelsChats,
    ChatContext,
    ConfirmationOption,
    DeclaresCompletionTriggers,
    ForkedFrom,
    HostsChats,
    ToolConfirmationOutcome,
    UpdatesAgentInfo,
    UserMessage,
)

from ahp_host_acp import permissions
from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec

from .fakes import FAKE_AGENT, RecordingSink, context

SESSION = "ahp-session:/1"
DEFAULT = "ahp-chat:/1"
SECOND = "ahp-chat:/2"


def _provider(root: Path, log: Path, *flags: str, catalogue: Path | None = None) -> AcpProvider:
    env = {"FAKE_ACP_LOG": str(log), **{f"FAKE_ACP_{flag}": "1" for flag in flags}}
    return AcpProvider(root, AgentSpec(FAKE_AGENT, env=env), catalogue_file=catalogue)


def _logged(log: Path, method: str) -> list[Any]:
    lines = log.read_text().splitlines() if log.exists() else []
    return [entry["params"] for entry in map(json.loads, lines) if entry["method"] == method]


async def _say(
    session: AcpSession, text: str, chat: str = DEFAULT, sink: RecordingSink | None = None
) -> RecordingSink:
    sink = sink or RecordingSink()
    await session.send_user_message(UserMessage(text=text, chat_uri=chat), sink)
    return sink


async def _whoami(session: AcpSession, chat: str = DEFAULT) -> dict[str, Any]:
    sink = await _say(session, "whoami", chat)
    result: dict[str, Any] = json.loads("".join(e[1] for e in sink.events if e[0] == "text"))
    return result


def _chat(uri: str = SECOND, **kwargs: Any) -> ChatContext:
    return ChatContext(session_uri=SESSION, chat_uri=uri, **kwargs)


def _fork_of(turns: int, chat: str = DEFAULT) -> dict[str, Any]:
    fork = ForkedFrom(
        session_uri=SESSION,
        turns=tuple({"id": f"t{n}"} for n in range(turns)),
        chat_uri=chat,
        turn_id=f"t{turns - 1}",
    )
    return {"origin": {"kind": "fork", "chat": chat, "turnId": fork.turn_id}, "fork": fork}


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "agent.log"


# -- one ACP session per chat ---------------------------------------------------------


async def test_each_chat_is_its_own_conversation_in_one_process(tmp_path: Path, log: Path) -> None:
    session = await _provider(tmp_path, log).create_session(context(tmp_path))
    assert isinstance(session, HostsChats)
    assert isinstance(session, CancelsChats)
    try:
        await _say(session, "hello")
        await session.chat_opened(_chat())
        await _say(session, "echo two", SECOND)
        first, second = await _whoami(session), await _whoami(session, SECOND)
    finally:
        await session.aclose()
    assert first["session"] != second["session"]
    assert (first["history"], second["history"]) == (["hello"], ["echo two"])
    assert len(_logged(log, "initialize")) == 1  # one agent process for both
    assert len(_logged(log, "session/new")) == 2


async def test_a_chat_is_forked_with_the_agents_session_fork(tmp_path: Path, log: Path) -> None:
    session = await _provider(tmp_path, log, "FORK").create_session(context(tmp_path))
    try:
        await _say(session, "hello")
        await session.chat_opened(_chat(**_fork_of(1)))
        forked = await _whoami(session, SECOND)
        source = await _whoami(session)
    finally:
        await session.aclose()
    assert forked["history"] == ["hello"]  # what the fork shows, the agent knows
    assert forked["session"] != source["session"]
    (fork,) = _logged(log, "session/fork")
    assert fork["sessionId"] == source["session"]


async def test_a_fork_that_would_not_match_the_agents_history_is_refused(
    tmp_path: Path, log: Path
) -> None:
    session = await _provider(tmp_path, log, "FORK").create_session(context(tmp_path))
    try:
        await _say(session, "hello")
        await _say(session, "echo again")
        # ACP forks the whole conversation: not at the first of two turns.
        with pytest.raises(AhpError, match="latest turn") as refused:
            await session.chat_opened(_chat(**_fork_of(1)))
        assert refused.value.code == -32009
        assert _logged(log, "session/fork") == []
    finally:
        await session.aclose()


async def test_no_fork_without_the_agents_support(tmp_path: Path, log: Path) -> None:
    session = await _provider(tmp_path, log).create_session(context(tmp_path))
    try:
        await _say(session, "hello")
        with pytest.raises(AhpError, match="cannot fork"):
            await session.chat_opened(_chat(**_fork_of(1)))
        with pytest.raises(AhpError, match="side chats"):
            await session.chat_opened(_chat(origin={"kind": "sideChat", "chat": DEFAULT}))
    finally:
        await session.aclose()


async def test_cancel_chat_stops_only_that_chats_session(tmp_path: Path, log: Path) -> None:
    session = await _provider(tmp_path, log).create_session(context(tmp_path))
    try:
        await _say(session, "hello")
        await session.chat_opened(_chat())
        turn = asyncio.create_task(_say(session, "slow", SECOND))
        for _ in range(200):
            if len(_logged(log, "session/prompt")) >= 2:
                break
            await asyncio.sleep(0.05)
        await session.cancel_chat(SECOND, "stopped")
        await asyncio.wait_for(turn, 10)
        second = await _whoami(session, SECOND)
    finally:
        await session.aclose()
    assert [c["sessionId"] for c in _logged(log, "session/cancel")] == [second["session"]]


@pytest.mark.parametrize("close", [True, False])
async def test_a_closed_chat_closes_its_session(tmp_path: Path, log: Path, close: bool) -> None:
    flags = ("CLOSE",) if close else ()
    session = await _provider(tmp_path, log, *flags).create_session(context(tmp_path))
    try:
        await session.chat_opened(_chat())
        second = await _whoami(session, SECOND)
        await session.chat_closed(SECOND)
        await session.chat_closed(DEFAULT)  # never: it lives as long as the session
        assert (await _whoami(session))["session"] != second["session"]
    finally:
        await session.aclose()
    closed = [c["sessionId"] for c in _logged(log, "session/close")]
    assert closed == ([second["session"]] if close else [])
    assert _logged(log, "session/cancel") == []  # it was not mid-turn


async def test_chats_resume_their_own_sessions(tmp_path: Path, log: Path) -> None:
    provider = _provider(tmp_path, log)
    first = await provider.create_session(context(tmp_path))
    await first.chat_opened(_chat())
    before = await _whoami(first, SECOND)
    state = await provider.resume_state_of(first)
    await first.aclose()
    assert state is not None
    assert state["chats"][SECOND] == {"acpSessionId": before["session"], "turns": 1}

    again = await provider.resume_session(context(tmp_path, resume_state=state))
    await again.chat_opened(_chat(restored=True))
    try:
        after = await _whoami(again, SECOND)
    finally:
        await again.aclose()
    assert (after["session"], after["opened"]) == (before["session"], "session/resume")


async def test_a_config_change_reaches_every_chat(tmp_path: Path, log: Path) -> None:
    catalogue = tmp_path / "agent.json"
    provider = _provider(tmp_path, log, "CONFIG_OPTIONS", catalogue=catalogue)
    warm = await provider.create_session(context(tmp_path))
    await _whoami(warm)
    await warm.aclose()
    session = await provider.create_session(context(tmp_path))
    try:
        await session.chat_opened(_chat())
        ids = {(await _whoami(session))["session"], (await _whoami(session, SECOND))["session"]}
        await session.config_changed({"mode": "code"})
    finally:
        await session.aclose()
    sent = {
        c["sessionId"] for c in _logged(log, "session/set_config_option") if c["value"] == "code"
    }
    assert sent == ids


# -- permission options ------------------------------------------------------------------


async def test_the_agents_own_options_are_offered(tmp_path: Path, log: Path) -> None:
    session = await _provider(tmp_path, log).create_session(context(tmp_path))
    try:
        sink = await _say(session, "tool")
    finally:
        await session.aclose()
    (asked,) = sink.confirmations
    assert asked.options == (
        ConfirmationOption("always", "Always", "approve", 1),
        ConfirmationOption("once", "Once", "approve", 1),
        ConfirmationOption("no", "No", "deny", 2),
        ConfirmationOption("never", "Always reject", "deny", 2),  # a blank name is filled in
    )  # an option of a kind ACP does not define is not offered


@pytest.mark.parametrize(
    ("sink", "chose"),
    [
        (RecordingSink(pick="always"), "always"),  # the user chose it: the agent gets it
        (RecordingSink(approve=False, pick="never"), "never"),
        (RecordingSink(approve=True), "once"),  # a plain yes is the narrowest one
        (RecordingSink(approve=False, reason_message="not that file"), "no"),
    ],
)
async def test_the_agent_gets_exactly_what_the_user_picked(
    tmp_path: Path, log: Path, sink: RecordingSink, chose: str
) -> None:
    session = await _provider(tmp_path, log).create_session(context(tmp_path))
    try:
        await _say(session, "tool", sink=sink)
    finally:
        await session.aclose()
    assert "".join(e[1] for e in sink.events if e[0] == "text") == f"chose {chose}"


def test_nothing_broader_than_the_answer_is_ever_picked() -> None:
    always_only = permissions.acp_options(
        [{"optionId": "a", "name": "Always", "kind": "allow_always"}]
    )
    # A plain approval where the agent offers no "once": nothing is granted.
    assert permissions.answer(always_only, ToolConfirmationOutcome(approved=True)) == (
        permissions.CANCELLED
    )
    # An option that disagrees with the answer grants nothing either.
    picked = ConfirmationOption("a", "Always", "approve")
    outcome = ToolConfirmationOutcome(approved=False, selected_option=picked)
    assert permissions.answer(always_only, outcome) == permissions.CANCELLED


# -- fileEdit results ----------------------------------------------------------------------


async def test_an_edit_is_previewed_then_shown_as_a_file_edit(tmp_path: Path, log: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("one\n")
    session = await _provider(tmp_path, log).create_session(context(tmp_path))
    try:
        sink = await _say(session, "askedit")
    finally:
        await session.aclose()
    (asked,) = sink.confirmations
    assert [(e.uri, e.before, e.after) for e in asked.edits] == [
        (notes.as_uri(), b"one\n", b"one\nasked\n")
    ]
    assert [(e.before, e.after) for e in sink.file_edits] == [(b"one\n", b"one\nasked\n")]
    (completed,) = [e for e in sink.events if e[0] == "completed"]
    # The diff, not a text copy of it.
    assert completed[3] == {"content": [{"type": "fileEdit", "after": {"uri": notes.as_uri()}}]}


async def test_a_failed_edit_keeps_its_text_and_shows_no_diff(tmp_path: Path, log: Path) -> None:
    (tmp_path / "notes.txt").write_text("one\n")
    session = await _provider(tmp_path, log).create_session(context(tmp_path))
    try:
        sink = await _say(session, "badedit")
    finally:
        await session.aclose()
    assert sink.file_edits == []
    (completed,) = [e for e in sink.events if e[0] == "completed"]
    assert completed[3]["content"][0]["text"].startswith("--- ")


# -- AgentInfo ----------------------------------------------------------------------------


async def test_agent_info_follows_the_agent(tmp_path: Path, log: Path) -> None:
    provider = _provider(tmp_path, log, "FORK", catalogue=tmp_path / "agent.json")
    assert isinstance(provider, UpdatesAgentInfo)
    assert isinstance(provider, DeclaresCompletionTriggers)
    assert Host(provider, LoopbackSingleUserPolicy()).completion_triggers() == ("/",)
    calls: list[Mapping[str, Any]] = []

    async def changed() -> bool:
        calls.append(provider.agent.to_wire())
        return True

    await provider.attach_agent_updates(changed)
    assert provider.agent.capabilities == {"multipleChats": {}}
    session = await provider.create_session(context(tmp_path))
    try:
        await _whoami(session)
    finally:
        await session.aclose()
    # The agent said it can fork: from now on (and after a restart), so may chats.
    assert calls[0]["capabilities"] == {"multipleChats": {"fork": True}}
    again = _provider(tmp_path, log, catalogue=tmp_path / "agent.json")
    assert again.agent.capabilities == {"multipleChats": {"fork": True}}
