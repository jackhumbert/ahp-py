"""Continuing another Claude Code conversation: pick, fork, resume, recap."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agent_host_server.provider.base import AgentSessionContext, ConfigRequest, UserMessage
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SDKSessionInfo

from agent_host_server_claude.provider import ClaudeProvider
from agent_host_server_claude.sessions import ClaudeCodeSessions
from tests.fakes import FakeClient, RecordingSink

ORIGINAL = "11111111-2222-3333-4444-555555555555"
FORKED = "99999999-8888-7777-6666-555555555555"
OUTSIDE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _info(session_id: str, cwd: Path, title: str, minutes_ago: int) -> SDKSessionInfo:
    return SDKSessionInfo(
        session_id=session_id,
        summary=title,
        last_modified=int((time.time() - minutes_ago * 60) * 1000),
        cwd=str(cwd),
        git_branch="main",
        first_prompt=f"please {title.lower()}",
    )


class Catalogue:
    """Fake SDK session functions over a fixed set of sessions."""

    def __init__(self, root: Path) -> None:
        project = root / "proj"
        project.mkdir(exist_ok=True)
        self.project = project
        self.sessions = {
            ORIGINAL: _info(ORIGINAL, project, "Fix the login bug", 5),
            OUTSIDE: _info(OUTSIDE, root.parent, "Something elsewhere", 1),
            "bbbbbbbb-0000-0000-0000-000000000000": _info(
                "bbbbbbbb-0000-0000-0000-000000000000", project, "Write release notes", 60
            ),
        }
        self.forks: list[tuple[str, dict[str, Any]]] = []

    def list_fn(self) -> list[SDKSessionInfo]:
        return list(self.sessions.values())

    def info_fn(self, session_id: str) -> SDKSessionInfo | None:
        return self.sessions.get(session_id)

    def fork_fn(self, session_id: str, **kwargs: Any) -> Any:
        self.forks.append((session_id, kwargs))
        return SimpleNamespace(session_id=FORKED)

    def messages_fn(self, session_id: str, **kwargs: Any) -> list[Any]:
        return [
            SimpleNamespace(type="user", message={"content": "fix it"}),
            SimpleNamespace(
                type="assistant",
                message={"content": [{"type": "text", "text": "Fixed in auth.py.\n\nNext?"}]},
            ),
        ]

    def catalogue(self, root: Path) -> ClaudeCodeSessions:
        return ClaudeCodeSessions(
            root,
            list_fn=self.list_fn,
            info_fn=self.info_fn,
            fork_fn=self.fork_fn,
            messages_fn=self.messages_fn,
        )


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=FORKED,
    )


@pytest.fixture
def setup(tmp_path: Path) -> tuple[ClaudeProvider, Catalogue, list[FakeClient]]:
    root = tmp_path / "root"
    root.mkdir()
    fake = Catalogue(root)
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        clients.append(FakeClient(options, [[_result()], [_result()]]))
        return clients[-1]

    provider = ClaudeProvider(root, client_factory=factory, sessions=fake.catalogue(root))
    return provider, fake, clients


async def test_the_picker_offers_recent_sessions_inside_the_root(
    setup: tuple[ClaudeProvider, Catalogue, list[FakeClient]],
) -> None:
    provider, _, _ = setup
    resolution = await provider.resolve_config(ConfigRequest())
    prop = resolution.properties["continueFrom"]
    assert prop["enumDynamic"] is True
    assert prop["enum"][0] == "new"
    # Newest first; the one whose folder is outside the root is not offered.
    assert prop["enumLabels"] == ["New conversation", "Fix the login bug", "Write release notes"]
    assert prop["enumDescriptions"][1].startswith("proj · main · ")
    assert resolution.values["continueFrom"] == "new"


async def test_searching_filters_by_title_prompt_branch_or_folder(
    setup: tuple[ClaudeProvider, Catalogue, list[FakeClient]],
) -> None:
    provider, _, _ = setup
    found = await provider.complete_config(ConfigRequest(property="continueFrom", query="release"))
    assert [v.label for v in found] == ["Write release notes"]
    assert await provider.complete_config(ConfigRequest(property="permissionMode")) == ()


async def test_continuing_forks_resumes_in_its_folder_and_recaps(
    setup: tuple[ClaudeProvider, Catalogue, list[FakeClient]],
) -> None:
    provider, fake, clients = setup
    session = await provider.create_session(
        AgentSessionContext(
            session_uri="s", chat_uri="c", provider_id="claude", config={"continueFrom": ORIGINAL}
        )
    )
    # The original is forked, never resumed directly.
    assert fake.forks == [
        (ORIGINAL, {"directory": str(fake.project), "title": "Fix the login bug (continued)"})
    ]
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="and the tests?"), sink)
    assert clients[0].options.resume == FORKED
    assert clients[0].options.cwd == str(fake.project.resolve())
    first = sink.events[0]
    assert first[0] == "text"
    assert "Continuing “Fix the login bug”" in first[1]
    assert "> Fixed in auth.py.\n>\n> Next?" in first[1]
    # Only once.
    await session.send_user_message(UserMessage(text="thanks"), sink)
    assert sum(1 for e in sink.events if e[0] == "text" and "Continuing" in e[1]) == 1

    state = await provider.resume_state_of(session)
    assert state is not None
    assert state["claudeSessionId"] == FORKED
    assert state["cwd"] == str(fake.project.resolve())
    resumed = await provider.resume_session(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude", resume_state=state)
    )
    assert resumed.working_directory() == fake.project.resolve()


async def test_a_session_outside_the_root_cannot_be_continued(
    setup: tuple[ClaudeProvider, Catalogue, list[FakeClient]],
) -> None:
    provider, fake, _ = setup
    with pytest.raises(PermissionError, match="outside this host's root"):
        await provider.create_session(
            AgentSessionContext(
                session_uri="s",
                chat_uri="c",
                provider_id="claude",
                config={"continueFrom": OUTSIDE},
            )
        )
    assert fake.forks == []


async def test_new_or_garbage_starts_fresh(
    setup: tuple[ClaudeProvider, Catalogue, list[FakeClient]],
) -> None:
    provider, fake, _ = setup
    for value in ("new", "../../etc", 7, None):
        session = await provider.create_session(
            AgentSessionContext(
                session_uri="s", chat_uri="c", provider_id="claude", config={"continueFrom": value}
            )
        )
        assert session.claude_session_id is None
    assert fake.forks == []
