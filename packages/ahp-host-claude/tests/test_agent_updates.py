"""The picker, kept current after start-up (`UpdatesAgentInfo`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import (
    AgentSessionContext,
    ModelInfo,
    UpdatesAgentInfo,
    UserMessage,
)
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from ahp_host_claude import provider as provider_module
from ahp_host_claude.provider import ClaudeProvider
from tests.fakes import FakeClient, RecordingSink, eventually


class Notifier:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return True


async def test_a_learned_output_limit_reaches_the_picker_at_once(tmp_path: Path) -> None:
    usage: Any = {"claude-opus-5-5": {"contextWindow": 1_000_000, "maxOutputTokens": 128_000}}
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="abc",
        model_usage=usage,
    )
    model = ModelInfo(id="default", name="Default", meta={"resolvedModel": "claude-opus-5-5"})
    provider = ClaudeProvider(
        tmp_path,
        client_factory=lambda options: FakeClient(options, [[result]]),
        models=(model,),
        state_dir=tmp_path,
    )
    assert isinstance(provider, UpdatesAgentInfo)
    changed = Notifier()
    await provider.attach_agent_updates(changed)
    session = await provider.create_session(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude")
    )
    await session.send_user_message(UserMessage(text="x"), RecordingSink())
    await eventually(lambda: changed.calls == 1)
    (published,) = provider.agent.models
    assert isinstance(published, ModelInfo)
    assert published.max_output_tokens == 128_000
    assert published.max_context_window == 1_000_000


async def test_discovery_is_tried_again_when_start_up_found_no_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_module, "_REDISCOVERY", (0.0, 0.0))
    attempts: list[Any] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, [])
        attempts.append(client)
        if len(attempts) > 1:  # signed in by now
            client.server_info = {"models": [{"value": "default", "displayName": "Default"}]}
        return client

    provider = ClaudeProvider(tmp_path, client_factory=factory, rediscover=True)
    changed = Notifier()
    await provider.attach_agent_updates(changed)
    await eventually(lambda: changed.calls == 1)
    assert [m.id for m in provider.agent.models if isinstance(m, ModelInfo)] == ["default"]
    await provider.aclose()


async def test_no_retry_unless_asked(tmp_path: Path) -> None:
    provider = ClaudeProvider(tmp_path)
    await provider.attach_agent_updates(Notifier())
    assert provider._rediscovery is None
