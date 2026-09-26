"""Steering: a message sent into the turn in flight."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, UserMessage
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
)
from claude_agent_sdk import UserMessage as SdkUserMessage

from ahp_host_claude.provider import ClaudeProvider, ClaudeSession
from tests.fakes import FakeClient, RecordingSink


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )


def _context() -> AgentSessionContext:
    return AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude")


class _Steerer:
    """A script step that steers the session the way the host would."""

    def __init__(self) -> None:
        self.session: ClaudeSession | None = None
        self.taken: list[bool] = []

    def step(self, text: str) -> Any:
        async def run(options: ClaudeAgentOptions) -> None:
            assert self.session is not None
            self.taken.append(await self.session.steer("c", UserMessage(text=text)))

        return run


def _provider(tmp_path: Path, *turns: list[Any]) -> tuple[ClaudeProvider, list[FakeClient]]:
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        clients.append(FakeClient(options, list(turns)))
        return clients[-1]

    return ClaudeProvider(tmp_path, client_factory=factory), clients


def _texts(sink: RecordingSink) -> str:
    return "".join(e[1] for e in sink.events if e[0] == "text")


async def test_a_steered_message_joins_the_turn(tmp_path: Path) -> None:
    steerer = _Steerer()
    provider, clients = _provider(
        tmp_path,
        [
            steerer.step("and bananas"),
            SdkUserMessage(content="and bananas"),  # the CLI's replay: taken in
            AssistantMessage(content=[TextBlock("done, with bananas")], model="m"),
            _result(),
        ],
    )
    session = await provider.create_session(_context())
    steerer.session = session
    await session.send_user_message(UserMessage(text="go"), RecordingSink())

    assert steerer.taken == [True]
    steered = clients[0].prompts[-1]
    assert steered[0]["priority"] == "next"
    assert steered[0]["message"]["content"] == "and bananas"
    assert clients[0].options.extra_args == {"replay-user-messages": None}


async def test_an_answer_after_the_first_result_stays_in_the_turn(tmp_path: Path) -> None:
    """Steered after the last tool call: the CLI finishes, then answers it
    with a second result. The turn waits for it, so it doesn't spill into the
    next one."""
    steerer = _Steerer()
    provider, _ = _provider(
        tmp_path,
        [
            steerer.step("and bananas"),
            AssistantMessage(content=[TextBlock("first answer")], model="m"),
            _result(),
        ],
        [
            SdkUserMessage(content="and bananas"),
            AssistantMessage(content=[TextBlock(" and bananas")], model="m"),
            _result(),
        ],
    )
    session = await provider.create_session(_context())
    steerer.session = session
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="go"), sink)

    assert "first answer" in _texts(sink)
    assert "and bananas" in _texts(sink), "the steered answer was left for the next turn"


async def test_steering_is_refused_outside_a_turn(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path, [_result()])
    session = await provider.create_session(_context())
    assert await session.steer("c", UserMessage(text="hello")) is False
    await session.send_user_message(UserMessage(text="go"), RecordingSink())
    assert await session.steer("c", UserMessage(text="hello")) is False
