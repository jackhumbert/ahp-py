"""The hard cap over a real in-memory host: request usage stops a worker turn (M16).

A test-only outer provider drives `ProviderDispatcher` under a `Scheduler` directly, as
`test_provider_dispatcher.py` does without one, so the journal and the main chat's `agent`
tool call can be inspected.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from ahp_host import (
    AgentInfo,
    AgentSessionContext,
    Host,
    LoopbackSingleUserPolicy,
    TurnSink,
    UserMessage,
)

from ahp_host_flow.config import FlowConfig, parse_config
from ahp_host_flow.dispatch import ProviderDispatcher
from ahp_host_flow.engine import (
    BudgetExhaustedError,
    JournalWriter,
    RunHeader,
    RunOptions,
    Scheduler,
    load_journal,
)
from ahp_host_flow.loading import load_providers
from tests.hosting import connect, found, open_session, run_turn, shut, text_of
from tests.limits_fakes import ScriptedProvider

SESSION = "flow-livestop:/run-1"
RUN_ID = "livestop"


class StopSession:
    def __init__(self, config: FlowConfig, state_dir: Path, context: AgentSessionContext) -> None:
        self.config = config
        self.state_dir = state_dir
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        assert self.context.publisher is not None
        assert message.chat_uri is not None
        dispatcher = ProviderDispatcher(
            config=self.config,
            providers=load_providers(self.config),
            sink=sink,
            publisher=self.context.publisher,
            chat=message.chat_uri,
            session_uri=self.context.session_uri,
        )
        journal = JournalWriter.create(
            self.state_dir, RunHeader(RUN_ID, "<test>", None, False, None, 1000, 150)
        )
        scheduler = Scheduler(dispatcher, RunOptions(budget_total=150), journal=journal)
        reply: dict[str, str] = {}
        try:
            call, cached = scheduler.issue(message.text, {})
            reply["value"] = str(await scheduler.run(call, cached))
        except BudgetExhaustedError as exc:
            reply["error"] = str(exc)
        finally:
            scheduler.close()
            journal.close()
        await sink.text_delta(json.dumps(reply))

    async def cancel(self, reason: str | None = None) -> None:
        return None

    async def aclose(self) -> None:
        return None


class StopProvider:
    agent = AgentInfo(provider="stop", display_name="Stop", description="One capped call.")

    def __init__(self, config: FlowConfig, state_dir: Path) -> None:
        self.config = config
        self.state_dir = state_dir

    async def create_session(self, context: AgentSessionContext) -> StopSession:
        return StopSession(self.config, self.state_dir, context)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    ScriptedProvider.instances.clear()
    yield
    ScriptedProvider.instances.clear()


async def test_livestop_hosted_request_usage(tmp_path: Path) -> None:
    config = parse_config(
        {
            "providers": {"scripted": {"load": "tests.limits_fakes:ScriptedProvider"}},
            "routing": {"default": "scripted", "rules": []},
        },
        base=tmp_path,
    )
    host = Host(StopProvider(config, tmp_path / "state"), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, SESSION, "stop")
        turn = await run_turn(wire, chat, "t1", "requests:5:100:0.05")
        reply = json.loads(text_of(turn))
    finally:
        await shut(host, wire, serving)
    assert reply["error"].startswith("budget exhausted: 200 of 150 output tokens spent")
    (record,) = load_journal(tmp_path / "state", RUN_ID).agents
    assert (record.status, record.kind) == ("error", "budget")
    assert record.output_tokens == 200
    sessions = [s for p in ScriptedProvider.instances for s in p.sessions]
    assert len(sessions) == 1
    assert sessions[0].cancelled
    # The main chat's agent tool call ends as an error naming the budget, not left running.
    (call,) = found(turn, lambda m: m.get("toolName") == "agent")
    assert call["status"] == "completed"
    assert call["success"] is False
    assert call["pastTenseMessage"] == "agent() stopped"
    finished = call["_meta"]["ahp-py/flow"]["stepFinished"]
    assert finished["status"] == "error"
    assert finished["error"].startswith("stopped: budget exhausted: 200 of 150")
