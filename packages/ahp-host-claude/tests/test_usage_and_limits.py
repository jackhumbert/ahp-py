"""Per-turn usage, and what each model in the picker can take."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, UserMessage
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from ahp_host_claude.models import CACHE_FILE, Learned, Limits, probe_limits
from ahp_host_claude.provider import ClaudeProvider, discover, models_from_server_info
from ahp_host_claude.usage import report
from tests.fakes import FakeClient, RecordingSink

INFO = {
    "models": [
        {"value": "default", "resolvedModel": "claude-opus-5-5", "displayName": "Default"},
        {"value": "haiku", "resolvedModel": "claude-haiku-4-5", "displayName": "Haiku"},
    ],
    "commands": [{"name": "review", "description": "Review a change", "argumentHint": ""}],
}


def _result(**fields: Any) -> ResultMessage:
    base: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 3,
        "session_id": "abc",
    }
    return ResultMessage(**{**base, **fields})


class TestReport:
    def test_the_gauge_reads_the_last_requests_whole_prompt(self) -> None:
        turn = {
            "input_tokens": 30,
            "output_tokens": 900,
            "cache_read_input_tokens": 240_000,
            "cache_creation_input_tokens": 3_000,
        }
        last = {
            "input_tokens": 10,
            "output_tokens": 400,
            "cache_read_input_tokens": 90_000,
            "cache_creation_input_tokens": 1_000,
        }
        reported = report(turn, last, model="claude-opus-5-5")
        # Not the turn's sum, which counts the prompt once per request.
        assert reported.input_tokens == 91_010
        assert reported.cache_read_tokens == 90_000
        assert reported.output_tokens == 900  # what the turn generated
        assert reported.meta == {
            "cacheCreationTokens": 1_000,
            "turnTotals": {
                "inputTokens": 30,
                "outputTokens": 900,
                "cacheReadTokens": 240_000,
                "cacheCreationTokens": 3_000,
            },
        }

    def test_cost_and_the_models_limits_go_in_meta(self) -> None:
        reported = report(
            {"input_tokens": 5, "output_tokens": 2},
            None,
            model="claude-opus-5-5",
            model_usage={
                "claude-opus-5-5": {"contextWindow": 1_000_000, "maxOutputTokens": 64_000}
            },
            cost=0.25,
            total_cost=1.5,
        )
        assert reported.input_tokens == 5  # one request: the sum is the prompt
        assert reported.meta is not None
        assert reported.meta["costUsd"] == 0.25
        assert reported.meta["totalCostUsd"] == 1.5
        assert reported.meta["contextWindow"] == 1_000_000
        assert reported.meta["maxOutputTokens"] == 64_000


async def test_a_turn_reports_its_own_cost_not_the_running_total(tmp_path: Path) -> None:
    clients: list[FakeClient] = []

    def turn(total: float) -> list[Any]:
        return [
            AssistantMessage(
                content=[TextBlock("ok")],
                model="claude-opus-5-5",
                usage={"input_tokens": 4, "cache_read_input_tokens": 100},
            ),
            _result(usage={"input_tokens": 4, "output_tokens": 1}, total_cost_usd=total),
        ]

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        clients.append(FakeClient(options, [turn(0.5), turn(1.25)]))
        return clients[-1]

    provider = ClaudeProvider(tmp_path, client_factory=factory)
    session = await provider.create_session(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude")
    )
    first, second = RecordingSink(), RecordingSink()
    await session.send_user_message(UserMessage(text="a"), first)
    await session.send_user_message(UserMessage(text="b"), second)
    assert ("usage", 104, 1, 100, "claude-opus-5-5") in first.events
    assert first.usage_meta[0] is not None
    assert first.usage_meta[0]["costUsd"] == 0.5
    assert second.usage_meta[0] is not None
    assert second.usage_meta[0]["costUsd"] == 0.75
    assert second.usage_meta[0]["totalCostUsd"] == 1.25
    # Kept, so a restart measures the next turn from here.
    state = await provider.resume_state_of(session)
    assert state is not None
    assert state["costUsd"] == 1.25


async def test_a_resumed_session_with_no_baseline_reports_no_cost(tmp_path: Path) -> None:
    def factory(options: ClaudeAgentOptions) -> FakeClient:
        return FakeClient(options, [[_result(total_cost_usd=9.0)]])

    provider = ClaudeProvider(tmp_path, client_factory=factory)
    session = await provider.resume_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            resume_state={"claudeSessionId": "abc"},
        )
    )
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="x"), sink)
    assert sink.usage_meta[0] is not None
    assert "costUsd" not in sink.usage_meta[0]  # 9.0 includes turns from before
    assert sink.usage_meta[0]["totalCostUsd"] == 9.0


async def test_the_probe_asks_each_models_window(tmp_path: Path) -> None:
    probe = FakeClient(ClaudeAgentOptions(), [])
    probe.context_limits = {
        None: {"rawMaxTokens": 1_000_000, "maxTokens": 1_000_000},
        "haiku": {"rawMaxTokens": 200_000, "maxTokens": 180_000},
    }
    limits = await probe_limits(probe, INFO["models"])
    # "default" is the account default, which the SDK spells as no model.
    assert probe.models == [None, "haiku"]
    assert limits == {
        "default": Limits(context_window=1_000_000, max_prompt=1_000_000),
        "haiku": Limits(context_window=200_000, max_prompt=180_000),
    }


async def test_discovery_publishes_limits_and_vision(tmp_path: Path) -> None:
    Learned(tmp_path / CACHE_FILE).record(
        {"claude-haiku-4-5": {"contextWindow": 200_000, "maxOutputTokens": 32_000}}
    )

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, [])
        client.server_info = INFO
        client.context_limits = {None: {"rawMaxTokens": 1_000_000, "maxTokens": 1_000_000}}
        return client

    found = await discover(tmp_path, factory, state_dir=tmp_path)
    default, haiku = found.models
    assert (default.max_context_window, default.max_prompt_tokens) == (1_000_000, 1_000_000)
    assert default.max_output_tokens is None  # never used here: not guessed
    # The probe could not say; an earlier session's result could.
    assert (haiku.max_context_window, haiku.max_output_tokens) == (200_000, 32_000)
    assert haiku.max_prompt_tokens == 200_000
    assert default.supports_vision is True
    assert haiku.supports_vision is True
    assert default.to_wire("claude")["supportsVision"] is True
    assert [c["name"] for c in found.commands] == ["review"]


def test_models_without_limits_say_so() -> None:
    (model, _) = models_from_server_info(INFO)
    assert model.max_context_window is None
    assert model.max_prompt_tokens is None
    assert model.supports_vision is True


async def test_results_teach_the_next_start_up(tmp_path: Path) -> None:
    def factory(options: ClaudeAgentOptions) -> FakeClient:
        usage = {"claude-sonnet-5": {"contextWindow": 200_000, "maxOutputTokens": 64_000}}
        return FakeClient(options, [[_result(model_usage=usage)]])

    provider = ClaudeProvider(tmp_path, client_factory=factory, state_dir=tmp_path)
    session = await provider.create_session(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude")
    )
    await session.send_user_message(UserMessage(text="x"), RecordingSink())
    saved = json.loads((tmp_path / CACHE_FILE).read_text())
    assert saved == {"claude-sonnet-5": {"contextWindow": 200_000, "maxOutputTokens": 64_000}}
    entry = {"value": "sonnet", "resolvedModel": "claude-sonnet-5"}
    assert Learned(tmp_path / CACHE_FILE).for_entry(entry).max_output == 64_000


def test_an_unreadable_cache_is_nothing_learned(tmp_path: Path) -> None:
    (tmp_path / CACHE_FILE).write_text("{not json")
    assert Learned(tmp_path / CACHE_FILE).for_entry({"value": "x"}) == Limits()
