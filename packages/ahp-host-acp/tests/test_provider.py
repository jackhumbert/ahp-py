"""The adapter against a real subprocess speaking ACP (`fake_agent.py`)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from agent_host_server.provider.base import (
    AgentSessionContext,
    ModelInfo,
    ModelSelection,
    UserMessage,
)

from agent_host_server_acp.provider import AcpProvider, AcpSession, AgentSpec

from .fakes import FAKE_AGENT, RecordingSink

MODELS = (ModelInfo(id="glm", name="GLM"), ModelInfo(id="other", name="Other"))


def _context(root: Path, **kwargs: Any) -> AgentSessionContext:
    return AgentSessionContext(
        session_uri="ahp-session:/1",
        chat_uri="ahp-chat:/1",
        provider_id="acp",
        working_directories=(root.as_uri(),),
        **kwargs,
    )


def _provider(
    root: Path,
    log: Path,
    *,
    native: bool = False,
    model_command: str | None = "/model {model} -s",
    models: tuple[ModelInfo, ...] = MODELS,
) -> AcpProvider:
    env = {"FAKE_ACP_LOG": str(log)}
    if native:
        env["FAKE_ACP_NATIVE_MODELS"] = "1"
    return AcpProvider(
        root, AgentSpec(FAKE_AGENT, env=env, model_command=model_command), models=models
    )


def _methods(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [json.loads(line)["method"] for line in log.read_text().splitlines()]


def _texts(sink: RecordingSink) -> str:
    return "".join(event[1] for event in sink.events if event[0] == "text")


async def _whoami(session: AcpSession) -> dict[str, Any]:
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="whoami"), sink)
    result: dict[str, Any] = json.loads(_texts(sink))
    return result


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "agent.log"


@pytest.fixture
async def session(tmp_path: Path, log: Path) -> AsyncIterator[AcpSession]:
    s = await _provider(tmp_path, log).create_session(_context(tmp_path))
    yield s
    await s.aclose()


async def test_text_reasoning_and_usage_stream_through(session: AcpSession) -> None:
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="hello"), sink)
    assert ("reasoning", "thinking…") in sink.events
    assert _texts(sink) == "Hi there"
    assert ("usage", 1234, None, None, "glm") in sink.events
    assert not [e for e in sink.events if e[0] == "failed"]


async def test_default_model_is_switched_once_by_command_and_not_shown(
    session: AcpSession, log: Path
) -> None:
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="hello"), sink)
    assert "Model set" not in _texts(sink)
    await session.send_user_message(UserMessage(text="hello"), RecordingSink())
    who = await _whoami(session)
    assert who["model"] == "glm"
    assert _methods(log).count("session/prompt") == 4  # one /model, three turns


async def test_picking_another_model_switches_again(session: AcpSession) -> None:
    await session.send_user_message(UserMessage(text="hello"), RecordingSink())
    await session.send_user_message(
        UserMessage(text="hello", model=ModelSelection(id="other")), RecordingSink()
    )
    assert (await _whoami(session))["model"] == "other"


async def test_native_acp_models_use_set_model(tmp_path: Path, log: Path) -> None:
    session = await _provider(tmp_path, log, native=True).create_session(_context(tmp_path))
    try:
        assert (await _whoami(session))["model"] == "glm"
        assert "session/set_model" in _methods(log)
        assert _methods(log).count("session/prompt") == 1
    finally:
        await session.aclose()


async def test_no_models_means_no_switching(tmp_path: Path, log: Path) -> None:
    session = await _provider(tmp_path, log, models=()).create_session(_context(tmp_path))
    try:
        assert (await _whoami(session))["model"] == "agent-default"
    finally:
        await session.aclose()


async def test_approved_tool_call_runs_once_and_reads_well(session: AcpSession) -> None:
    sink = RecordingSink(approve=True)
    await session.send_user_message(UserMessage(text="tool"), sink)
    assert [c.call_id for c in sink.confirmations] == ["call_1"]
    assert sink.confirmations[0].tool_input == {"command": "echo hi", "title": "Run echo hi"}
    started = next(e for e in sink.events if e[0] == "started")
    assert started == ("started", "call_1", "execute", "Run command")
    assert sink.invocations["call_1"] == "Run echo hi"
    assert sink.outputs["call_1"] == [{"type": "text", "text": "hi"}]
    completed = next(e for e in sink.events if e[0] == "completed")
    assert completed[2] is True
    assert sink.past_tense["call_1"] == "Ran `echo hi`"
    # "Always allow" is never picked: approval here is one call at a time.
    assert _texts(sink) == "chose once"


async def test_declined_tool_call_is_rejected(session: AcpSession) -> None:
    sink = RecordingSink(approve=False)
    await session.send_user_message(UserMessage(text="tool"), sink)
    completed = next(e for e in sink.events if e[0] == "completed")
    assert completed[2] is False
    assert sink.past_tense["call_1"] == "Failed: Run echo hi"
    assert _texts(sink) == "chose no"


async def test_session_resumes_after_a_host_restart(tmp_path: Path, log: Path) -> None:
    provider = _provider(tmp_path, log)
    first = await provider.create_session(_context(tmp_path))
    before = await _whoami(first)
    state = await provider.resume_state_of(first)
    await first.aclose()
    assert state is not None
    assert state["acpSessionId"] == before["session"]

    again = await provider.resume_session(_context(tmp_path, resume_state=state))
    try:
        after = await _whoami(again)
    finally:
        await again.aclose()
    assert after["session"] == before["session"]
    assert after["opened"] == "session/resume"
    assert after["model"] == "glm"  # re-applied: the new process knew nothing of it


async def test_session_runs_in_its_folder(tmp_path: Path, log: Path) -> None:
    sub = tmp_path / "project"
    sub.mkdir()
    session = await _provider(tmp_path, log).create_session(_context(sub))
    try:
        assert Path((await _whoami(session))["cwd"]) == sub.resolve()
    finally:
        await session.aclose()


async def test_folder_outside_the_roots_is_refused(tmp_path: Path, log: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    session = await _provider(root, log).create_session(_context(tmp_path))
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="hello"), sink)
    assert sink.events[-1][0] == "failed"
    assert sink.events[-1][2] == "agent.workingDirectory"
    assert _methods(log) == []  # the agent was never started


async def test_crash_fails_the_turn_and_the_next_turn_restarts(session: AcpSession) -> None:
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="crash"), sink)
    failed = [e for e in sink.events if e[0] == "failed"]
    assert failed
    assert failed[0][2] == "acp.agentExited"
    assert "crashing on purpose" in failed[0][1]
    who = await _whoami(session)
    assert who["opened"] == "session/resume"


async def test_stop_cancels_the_prompt(session: AcpSession, log: Path) -> None:
    sink = RecordingSink()
    turn = asyncio.create_task(session.send_user_message(UserMessage(text="slow"), sink))
    for _ in range(200):
        if _methods(log).count("session/prompt") >= 2:  # the /model switch, then "slow"
            break
        await asyncio.sleep(0.05)
    turn.cancel()
    await session.cancel("stopped")
    with pytest.raises(asyncio.CancelledError):
        await turn
    # The next turn waits for the stopped one to wind down, then works.
    await session.send_user_message(UserMessage(text="hello"), sink2 := RecordingSink())
    assert _texts(sink2) == "Hi there"
    assert "session/cancel" in _methods(log)


async def test_unoffered_client_methods_are_refused(session: AcpSession) -> None:
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="fs"), sink)
    assert _texts(sink) == "fs error -32601"


async def test_stop_reason_other_than_end_turn_fails_the_turn(session: AcpSession) -> None:
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="limit"), sink)
    assert sink.events[-1] == ("failed", "The agent hit its output token limit.", "acp.max_tokens")


async def test_agent_that_cannot_start_fails_the_turn(tmp_path: Path) -> None:
    provider = AcpProvider(tmp_path, AgentSpec(("no-such-acp-agent-xyz",)))
    session = await provider.create_session(_context(tmp_path))
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="hello"), sink)
    assert sink.events[-1][0] == "failed"
    assert sink.events[-1][2] == "acp.start"


async def test_config_options_are_set_on_each_session_and_refusals_ignored(
    tmp_path: Path, log: Path
) -> None:
    spec = AgentSpec(
        FAKE_AGENT,
        env={"FAKE_ACP_LOG": str(log)},
        config_options={"thought_level": "low", "bogus": "x"},
    )
    provider = AcpProvider(tmp_path, spec)
    session = await provider.create_session(_context(tmp_path))
    try:
        assert (await _whoami(session))["options"] == {"thought_level": "low"}
        state = await provider.resume_state_of(session)
    finally:
        await session.aclose()
    again = await provider.resume_session(_context(tmp_path, resume_state=state))
    try:
        # A resumed session is a new process: the options are set again.
        assert (await _whoami(again))["options"] == {"thought_level": "low"}
    finally:
        await again.aclose()


def test_agent_info_offers_the_configured_models(tmp_path: Path) -> None:
    provider = AcpProvider(tmp_path, AgentSpec(FAKE_AGENT), models=MODELS, provider_id="openclaw")
    wire = provider.agent.to_wire()
    assert wire["provider"] == "openclaw"
    assert [m["id"] for m in wire["models"]] == ["glm", "other"]
