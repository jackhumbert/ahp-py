"""Resuming a turn that failed on something transient (`ResumesTurns`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, ResumesTurns, UserMessage
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from ahp_host_claude.provider import (
    RESUME_PROMPT,
    ClaudeProvider,
    ClaudeSession,
    LocalClaudeSession,
)
from tests.fakes import FakeClient, RecordingSink, text_of


def _failed(status: int | None, error: str = "API Error") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="abc",
        result=error,
        api_error_status=status,
    )


def _ok() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )


class Harness:
    def __init__(self, root: Path, *turns: list[Any]) -> None:
        self.turns = list(turns)
        self.clients: list[FakeClient] = []
        self.provider = ClaudeProvider(root, client_factory=self._factory)

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        self.clients.append(FakeClient(options, self.turns))
        return self.clients[-1]

    async def session(self) -> Any:
        return await self.provider.create_session(
            AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude")
        )


async def test_an_overloaded_api_is_offered_for_resuming(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_failed(529)], [_failed(400, "bad request")])
    session = await harness.session()
    assert isinstance(session, LocalClaudeSession)
    assert isinstance(session, ResumesTurns)
    overloaded, refused = RecordingSink(), RecordingSink()
    await session.send_user_message(UserMessage(text="a"), overloaded)
    await session.send_user_message(UserMessage(text="b"), refused)
    assert overloaded.resumable == [True]
    # The same request would fail the same way again.
    assert refused.resumable == [False]


async def test_an_api_error_on_the_answer_is_offered_too(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [AssistantMessage([TextBlock("…")], model="m", error="rate_limit"), _failed(None)],
    )
    session = await harness.session()
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="a"), sink)
    assert sink.resumable == [True]


async def test_resuming_asks_claude_to_carry_on_in_the_same_turn(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [AssistantMessage([TextBlock("half")], model="m", uuid="h1"), _failed(529)],
        [AssistantMessage([TextBlock(" and the rest")], model="m", uuid="h2"), _ok()],
    )
    session = await harness.session()
    await session.send_user_message(UserMessage(text="go"), RecordingSink(turn_id="t1"))
    resumed = RecordingSink(turn_id="t1")
    await session.resume_turn("c", "t1", resumed)
    assert text_of(harness.clients[0].prompts[1]) == RESUME_PROMPT
    assert ("text", " and the rest") in resumed.events
    # Still one turn here: its mark carries on to the resumed answer.
    assert [(m.turn, m.last) for m in session.marks] == [("t1", "h2")]


def test_a_claude_ai_session_cannot_resume(tmp_path: Path) -> None:
    mirror = ClaudeSession(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude"),
        root=tmp_path,
        client_factory=lambda o: FakeClient(o, []),
        mirror_of="cse_1",
    )
    assert not isinstance(mirror, ResumesTurns)
