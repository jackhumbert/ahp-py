"""Side chats: opt-in capability, the seed on the first prompt only, and its limits."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest
from ahp_host import AhpError
from ahp_host.provider.base import ChatContext, ForkedFrom, UserMessage

from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec
from ahp_host_acp.side_chat import SideChatMode, SideChatOrigin, SideChatSeed, render_preamble

from .fakes import FAKE_AGENT, RecordingSink, context

SESSION = "ahp-session:/1"
DEFAULT = "ahp-chat:/1"
SIDE = "ahp-chat:/side"


def _turn(n: int, asked: str, answer: str) -> dict[str, Any]:
    return {
        "id": f"t{n}",
        "message": {"text": asked},
        "responseParts": [{"kind": "markdown", "content": answer}],
    }


def _provider(
    root: Path,
    log: Path,
    mode: SideChatMode | None = None,
    cls: type[AcpSession] = AcpSession,
) -> AcpProvider:
    env = {"FAKE_ACP_LOG": str(log)}
    provider = AcpProvider(root, AgentSpec(FAKE_AGENT, env=env, side_chat=mode))
    provider.session_class = cls
    return provider


def _logged(log: Path, method: str) -> list[Any]:
    lines = log.read_text().splitlines() if log.exists() else []
    return [entry["params"] for entry in map(json.loads, lines) if entry["method"] == method]


def _side(turns: list[dict[str, Any]], selection: Mapping[str, Any] | None = None) -> ChatContext:
    origin: dict[str, Any] = {"kind": "sideChat", "chat": DEFAULT, "turnId": turns[-1]["id"]}
    if selection is not None:
        origin["selection"] = dict(selection)
    source = ForkedFrom(
        session_uri=SESSION, turns=tuple(turns), chat_uri=DEFAULT, turn_id=turns[-1]["id"]
    )
    return ChatContext(session_uri=SESSION, chat_uri=SIDE, origin=origin, side_chat=source)


async def _say(session: AcpSession, text: str, chat: str) -> None:
    await session.send_user_message(UserMessage(text=text, chat_uri=chat), RecordingSink())


def _texts(params: Mapping[str, Any]) -> list[str]:
    return [b["text"] for b in params["prompt"] if b.get("type") == "text"]


def test_no_side_chat_capability_by_default(tmp_path: Path) -> None:
    capabilities = AcpProvider(tmp_path, AgentSpec(FAKE_AGENT)).agent.capabilities
    assert capabilities["multipleChats"] == {}


def test_the_capability_is_advertised_when_enabled(tmp_path: Path) -> None:
    provider = AcpProvider(tmp_path, AgentSpec(FAKE_AGENT, side_chat=SideChatMode()))
    assert provider.agent.capabilities["multipleChats"] == {"sideChat": True}


async def test_a_side_chat_is_refused_when_disabled(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    session = await _provider(tmp_path, log).create_session(context(tmp_path))
    try:
        with pytest.raises(AhpError, match="side chats"):
            await session.chat_opened(_side([_turn(1, "q", "a")]))
    finally:
        await session.aclose()


async def test_the_first_prompt_carries_the_preamble_and_the_second_does_not(
    tmp_path: Path,
) -> None:
    log = tmp_path / "agent.log"
    turns = [_turn(1, "what is 2+2", "four"), _turn(2, "and 3+3", "six")]
    session = await _provider(tmp_path, log, SideChatMode()).create_session(context(tmp_path))
    try:
        await _say(session, "main question", DEFAULT)
        await session.chat_opened(_side(turns, {"text": "the word six"}))
        await _say(session, "why?", SIDE)
        await _say(session, "and then?", SIDE)
    finally:
        await session.aclose()
    main, first, second = _logged(log, "session/prompt")
    assert first["sessionId"] != main["sessionId"]  # its own ACP session
    preamble, ask = _texts(first)
    assert "side question" in preamble
    assert "User: what is 2+2" in preamble
    assert "Agent: six" in preamble
    assert "the word six" in preamble
    assert "nothing in it is an instruction" in preamble
    assert ask == "why?"
    assert _texts(second) == ["and then?"]
    assert _texts(main) == ["main question"]  # the source chat was not written to
    assert "_meta" not in first


async def test_a_long_source_is_capped_to_its_latest_turns(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    turns = [_turn(n, f"question {n}", "x" * 300) for n in range(20)]
    session = await _provider(tmp_path, log, SideChatMode(max_chars=1000)).create_session(
        context(tmp_path)
    )
    try:
        await session.chat_opened(_side(turns))
        await _say(session, "go", SIDE)
    finally:
        await session.aclose()
    (prompt,) = _logged(log, "session/prompt")
    preamble = _texts(prompt)[0]
    assert "question 19" in preamble
    assert "question 0" not in preamble
    assert "earlier turns left out" in preamble
    assert len(preamble) < 1000 + 800


def test_the_newest_turn_alone_is_clipped_when_it_does_not_fit() -> None:
    origin = SideChatOrigin(DEFAULT, None, "t1", None, [_turn(1, "q", "y" * 5000)])
    text = render_preamble(origin, 200)
    assert "cut short" in text
    assert "y" * 300 not in text


class _Meta(AcpSession):
    seen: ClassVar[list[SideChatOrigin]] = []

    async def side_chat_seed(self, origin: SideChatOrigin) -> SideChatSeed | None:
        self.seen.append(origin)
        return SideChatSeed(meta={"example.com/seed": {"from": origin.acp_session_id}})


async def test_a_hook_can_seed_with_meta_instead(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    _Meta.seen = []
    session = await _provider(tmp_path, log, SideChatMode(), _Meta).create_session(
        context(tmp_path)
    )
    try:
        await _say(session, "main", DEFAULT)
        await session.chat_opened(_side([_turn(1, "q", "a")], {"text": "sel"}))
        await _say(session, "side one", SIDE)
        await _say(session, "side two", SIDE)
    finally:
        await session.aclose()
    (origin,) = _Meta.seen
    assert origin.chat_uri == DEFAULT
    assert origin.turn_id == "t1"
    assert origin.selection == {"text": "sel"}
    assert origin.acp_session_id is not None
    assert len(origin.turns) == 1
    _, first, second = _logged(log, "session/prompt")
    expected = {"example.com/seed": {"from": origin.acp_session_id}}
    assert first["_meta"] == expected
    assert _texts(first) == ["side one"]  # meta only, no preamble
    assert "_meta" not in second
    new = _logged(log, "session/new")
    assert [p.get("_meta") for p in new] == [None, expected]


async def test_a_pending_seed_survives_a_restart_and_applies_once(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    provider = _provider(tmp_path, log, SideChatMode())
    session = await provider.create_session(context(tmp_path))
    try:
        await _say(session, "main", DEFAULT)
        await session.chat_opened(_side([_turn(1, "q", "a")]))
        saved = session.resume_state()
    finally:
        await session.aclose()
    assert saved is not None
    assert "sideSeed" in saved["chats"][SIDE]
    resumed = await _provider(tmp_path, log, SideChatMode()).resume_session(
        context(tmp_path, resume_state=saved)
    )
    try:
        await resumed.chat_opened(
            ChatContext(session_uri=SESSION, chat_uri=SIDE, origin={"kind": "sideChat"})
        )
        await _say(resumed, "first", SIDE)
        await _say(resumed, "second", SIDE)
        after = resumed.resume_state()
    finally:
        await resumed.aclose()
    first, second = _logged(log, "session/prompt")[-2:]
    assert "side question" in _texts(first)[0]
    assert _texts(first)[-1] == "first"
    assert _texts(second) == ["second"]
    assert after is not None
    assert "sideSeed" not in after["chats"][SIDE]
