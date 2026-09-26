"""A session with no folder is a chat; adding a folder later gives it tools there."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agent_host_server.provider.base import (
    AgentSessionContext,
    FollowsWorkingDirectories,
    UserMessage,
)
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from agent_host_server_claude.provider import CHAT_PROMPT, CHAT_TOOLS, ClaudeProvider
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


def _context(*dirs: str) -> AgentSessionContext:
    return AgentSessionContext(
        session_uri="claude:/s",
        chat_uri="claude:/s/chat",
        provider_id="claude",
        working_directories=dirs,
    )


class Harness:
    def __init__(self, tmp_path: Path, *turns: list[Step]) -> None:
        self.root = tmp_path / "root"
        self.project = self.root / "project"
        self.other = self.root / "other"
        self.project.mkdir(parents=True)
        self.other.mkdir()
        self.chat_dir = tmp_path / "chat"
        self.turns = list(turns)
        self.clients: list[FakeClient] = []
        self.provider = ClaudeProvider(
            self.root, client_factory=self._factory, chat_dir=self.chat_dir
        )

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.turns)
        self.clients.append(client)
        return client


def _is_chat(options: ClaudeAgentOptions) -> bool:
    return options.tools == list(CHAT_TOOLS) and options.strict_mcp_config is True


async def test_the_agent_lets_a_client_add_folders(tmp_path: Path) -> None:
    provider = ClaudeProvider(tmp_path)
    assert provider.agent.capabilities["multipleWorkingDirectories"] == {"immutablePrimary": True}


async def test_a_session_with_no_folder_is_a_chat(tmp_path: Path) -> None:
    """Security: no file, shell or MCP tools, in an empty directory of its own
    - never the served root."""
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context())
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    options = harness.clients[0].options
    assert _is_chat(options)
    assert options.cwd == str(harness.chat_dir.resolve())
    assert harness.chat_dir.is_dir()
    assert options.add_dirs == []
    assert isinstance(options.system_prompt, dict)
    assert options.system_prompt.get("append") == CHAT_PROMPT


async def test_a_session_with_a_folder_has_its_tools_there(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context(harness.project.as_uri()))
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    options = harness.clients[0].options
    assert not _is_chat(options)
    assert options.tools is None
    assert options.cwd == str(harness.project.resolve())
    assert isinstance(options.system_prompt, dict)
    assert "append" not in options.system_prompt


async def test_a_folder_added_to_a_chat_gives_it_tools_there(tmp_path: Path) -> None:
    """The client restarts, resuming the same conversation in the same `cwd`
    (Claude Code stores it there), with the folder added beside it."""
    harness = Harness(tmp_path, [_result()], [_result()])
    session = await harness.provider.create_session(_context())
    assert isinstance(session, FollowsWorkingDirectories)
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())

    await session.working_directories_changed([harness.project.as_uri()])
    assert harness.clients[0].disconnected, "an idle client restarts straight away"

    await session.send_user_message(UserMessage(text="look at it"), RecordingSink())
    options = harness.clients[1].options
    assert not _is_chat(options)
    assert options.resume == "abc"
    assert options.cwd == str(harness.chat_dir.resolve())
    assert options.add_dirs == [harness.project.resolve()]


async def test_a_removed_folder_is_no_longer_granted(tmp_path: Path) -> None:
    """Only a folder after the first: the first is immutable (the capability),
    so the host refuses to remove it before it gets here."""
    harness = Harness(tmp_path, [_result()], [_result()])
    session = await harness.provider.create_session(_context())
    await session.working_directories_changed([harness.project.as_uri(), harness.other.as_uri()])
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    assert harness.clients[0].options.add_dirs == [harness.other.resolve()]

    await session.working_directories_changed([harness.project.as_uri()])
    await session.send_user_message(UserMessage(text="and now?"), RecordingSink())
    assert harness.clients[1].options.add_dirs == []


async def test_a_session_started_in_a_folder_keeps_it_as_its_cwd(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()], [_result()])
    session = await harness.provider.create_session(_context(harness.project.as_uri()))
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    await session.working_directories_changed([harness.project.as_uri(), harness.other.as_uri()])
    await session.send_user_message(UserMessage(text="both"), RecordingSink())
    options = harness.clients[1].options
    assert options.cwd == str(harness.project.resolve())
    assert options.add_dirs == [harness.other.resolve()]


async def test_a_change_that_changes_nothing_does_not_restart(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context(harness.project.as_uri()))
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    await session.working_directories_changed([harness.project.as_uri()])
    assert not harness.clients[0].disconnected


async def test_a_folder_added_mid_turn_waits_for_the_turn(tmp_path: Path) -> None:
    """Restarting would kill the turn in flight; the next one gets the folder."""
    harness = Harness(tmp_path)
    session = await harness.provider.create_session(_context())
    seen: list[bool] = []

    async def add_folder(options: ClaudeAgentOptions) -> None:
        await session.working_directories_changed([harness.project.as_uri()])
        seen.append(harness.clients[0].disconnected)

    harness.turns.extend([[add_folder, _result()], [_result()]])
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="hi"), sink)
    assert seen == [False]
    assert "failed" not in [event[0] for event in sink.events]

    await session.send_user_message(UserMessage(text="now"), RecordingSink())
    assert harness.clients[0].disconnected
    assert not _is_chat(harness.clients[1].options)


async def test_a_chat_resumes_in_its_own_directory(tmp_path: Path) -> None:
    """The `cwd` is in the resume state: a chat that gained a folder must not
    come back in that folder, where Claude Code has no record of it."""
    harness = Harness(tmp_path, [_result()], [_result()])
    session = await harness.provider.create_session(_context())
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    state = await harness.provider.resume_state_of(session)
    assert state is not None

    resumed = await harness.provider.resume_session(
        AgentSessionContext(
            session_uri="claude:/s",
            chat_uri="claude:/s/chat",
            provider_id="claude",
            working_directories=(harness.project.as_uri(),),
            resume_state=state,
        )
    )
    await resumed.send_user_message(UserMessage(text="back"), RecordingSink())
    options = harness.clients[1].options
    assert options.cwd == str(harness.chat_dir.resolve())
    assert options.add_dirs == [harness.project.resolve()]
    assert not _is_chat(options)


@pytest.mark.parametrize("outside", ["sibling", "parent"])
async def test_a_folder_outside_the_root_fails_the_next_turn(tmp_path: Path, outside: str) -> None:
    """The host refuses these first; one that gets here anyway is refused, not dropped."""
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.create_session(_context())
    elsewhere = tmp_path / "elsewhere" if outside == "sibling" else tmp_path
    elsewhere.mkdir(exist_ok=True)
    await session.working_directories_changed([elsewhere.as_uri()])
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="x"), sink)
    events: list[Any] = [event[0] for event in sink.events]
    assert events == ["failed"]
    assert harness.clients == []
