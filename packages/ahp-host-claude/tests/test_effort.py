"""Effort is a session setting: passed to Claude Code at start-up, changed by a restart."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, ConfigRequest, UserMessage
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from ahp_host_claude.effort import LEVELS
from ahp_host_claude.provider import ClaudeProvider
from tests.fakes import FakeClient, RecordingSink, Step


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=5,
        duration_api_ms=4,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )


def _context(config: dict[str, Any] | None = None) -> AgentSessionContext:
    return AgentSessionContext(
        session_uri="claude:/s",
        chat_uri="claude:/s/chat",
        provider_id="claude",
        working_directories=(),
        config=config or {},
    )


class Harness:
    def __init__(self, tmp_path: Path, *turns: list[Step]) -> None:
        self.turns = list(turns)
        self.clients: list[FakeClient] = []
        self.provider = ClaudeProvider(
            tmp_path, client_factory=self._factory, chat_dir=tmp_path / "chat"
        )

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.turns)
        self.clients.append(client)
        return client


async def test_effort_reaches_claude_code(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context({"effort": "high"}))
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    assert harness.clients[0].options.effort == "high"


async def test_default_and_unknown_effort_leave_it_to_claude_code(tmp_path: Path) -> None:
    for value in (None, "default", "extreme", 3):
        harness = Harness(tmp_path, [_result()])
        session = await harness.provider.create_session(_context({"effort": value}))
        await session.send_user_message(UserMessage(text="hi"), RecordingSink())
        assert harness.clients[0].options.effort is None


async def test_changing_effort_restarts_on_the_same_conversation(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()], [_result()])
    session = await harness.provider.create_session(_context())
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    await session.config_changed({"effort": "max"})
    assert harness.clients[0].disconnected
    await session.send_user_message(UserMessage(text="again"), RecordingSink())
    assert harness.clients[-1].options.effort == "max"
    assert harness.clients[-1].options.resume == "abc"


async def test_the_same_effort_restarts_nothing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context({"effort": "low"}))
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    await session.config_changed({"effort": "low"})
    assert not harness.clients[0].disconnected


async def test_effort_is_published_and_survives_a_resume(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    resolved = await harness.provider.resolve_config(
        ConfigRequest(provider="claude", values={"effort": "xhigh"})
    )
    assert resolved.properties["effort"]["enum"] == ["default", *LEVELS]
    assert resolved.properties["effort"]["sessionMutable"] is True
    assert resolved.values["effort"] == "xhigh"
    session = await harness.provider.create_session(_context({"effort": "xhigh"}))
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    state = await harness.provider.resume_state_of(session)
    assert state is not None
    assert state["effort"] == "xhigh"
