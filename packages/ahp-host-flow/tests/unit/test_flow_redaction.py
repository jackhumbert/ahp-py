"""`ahp_host_flow.redaction` without Node: chats registered by hand, a journal on disk."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.core.redaction import RedactionError as HostRedactionError
from ahp_host.provider.echo import EchoProvider
from ahp_protocol.ext import flow

from ahp_host_flow import FlowProvider, load_config
from ahp_host_flow.engine.journal import AgentRecord, JournalWriter, RunHeader
from ahp_host_flow.redaction import (
    REQUEST_SUFFIX,
    RedactionError,
    descendants,
    find_step,
    process_requests,
    queue_request,
    redact_step,
    result_path,
    start_poller,
)

RUN = "run-1"
SECRET = "PLAINLEAK-4242"
MAIN = "ahp-chat://main"
WORKER = "ahp-chat://worker"
TEXT = f"You said: say {SECRET}"
ACTOR = "test operator"

ECHO_TOML = """\
state_dir = "state"

[providers.echo]
load = "ahp_host.provider.echo:EchoProvider"
"""


def main_state() -> dict[str, Any]:
    step = flow.step(
        flow.step_started(
            ordinal=0, label="leak", phase=None, chat=WORKER, model=None, provider="echo"
        ),
        flow.step_finished(status="ok", result=TEXT, input_tokens=1, output_tokens=2),
    )
    return {
        "turns": [
            {
                "id": "T",
                "state": "complete",
                "message": {"text": "run it", "origin": {"kind": "user"}},
                "responseParts": [
                    {
                        "kind": "systemNotification",
                        "content": "run started",
                        "_meta": flow.meta(flow.run_started(RUN, "w")),
                    },
                    {
                        "kind": "toolCall",
                        "toolCall": {
                            "toolCallId": "agent-0",
                            "toolName": "agent",
                            "status": "completed",
                            "content": [{"type": "text", "text": TEXT}],
                            "_meta": flow.meta(step),
                        },
                    },
                ],
            }
        ],
        "activeTurn": None,
    }


def worker_state() -> dict[str, Any]:
    return {
        "turns": [
            {
                "id": "W",
                "state": "complete",
                "message": {"text": "say", "origin": {"kind": "user"}},
                "responseParts": [
                    {"kind": "markdown", "id": "p1", "content": TEXT},
                    {"kind": "systemNotification", "content": "note"},
                ],
            }
        ],
        "activeTurn": None,
    }


def write_journal(state_dir: Path, run_id: str = RUN, resumed_from: str | None = None) -> Path:
    writer = JournalWriter.create(
        state_dir, RunHeader(run_id, "<library>", None, False, None, 16, None, resumed_from)
    )
    writer.append_agent(AgentRecord(0, "k", "ok", TEXT))
    writer.close()
    return state_dir / "runs" / run_id / "journal.jsonl"


async def booted() -> Host:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    await host.sequencer.register_channel(MAIN, main_state(), "chat")
    await host.sequencer.register_channel(WORKER, worker_state(), "chat")
    return host


async def test_flow_redaction_step(tmp_path: Path) -> None:
    host = await booted()
    journal = write_journal(tmp_path / "state")
    assert find_step(host, RUN, 0) is not None
    report = await redact_step(
        host, state_dir=tmp_path / "state", run_id=RUN, ordinal=0, reason="leak", actor=ACTOR
    )
    assert report.worker_parts == 1
    assert report.skipped_parts == 1
    assert report.tool_call_id == "agent-0"
    assert report.main_chat == MAIN
    for uri in (MAIN, WORKER):
        assert SECRET not in json.dumps(host.sequencer.state_of(uri))
    assert flow.run_view(host.sequencer.state_of(MAIN)).steps[0].result == "[redacted]"
    assert SECRET not in journal.read_text(encoding="utf-8")
    assert report.to_json()["journal"] is True


async def test_flow_redaction_refuses_a_live_run(tmp_path: Path) -> None:
    host = await booted()
    journal = write_journal(tmp_path / "state")
    before = journal.read_text(encoding="utf-8")
    with pytest.raises(RedactionError, match="still running"):
        await redact_step(
            host,
            state_dir=tmp_path / "state",
            run_id=RUN,
            ordinal=0,
            reason="leak",
            actor=ACTOR,
            live_runs={RUN},
        )
    assert journal.read_text(encoding="utf-8") == before


async def test_flow_redaction_unknown_ordinal(tmp_path: Path) -> None:
    host = await booted()
    write_journal(tmp_path / "state")
    with pytest.raises(RedactionError):
        await redact_step(
            host, state_dir=tmp_path / "state", run_id=RUN, ordinal=9, reason="x", actor=ACTOR
        )


async def test_flow_redaction_journal_only(tmp_path: Path) -> None:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    journal = write_journal(tmp_path / "state")
    report = await redact_step(
        host, state_dir=tmp_path / "state", run_id=RUN, ordinal=0, reason="leak", actor=ACTOR
    )
    assert report.main_chat is None
    assert report.tool_call_id is None
    assert report.worker_chat is None
    assert SECRET not in journal.read_text(encoding="utf-8")


async def test_flow_redaction_lists_descendants(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    write_journal(state_dir)
    write_journal(state_dir, "run-2", resumed_from=RUN)
    write_journal(state_dir, "run-3", resumed_from="run-2")
    assert descendants(state_dir, RUN) == ("run-2", "run-3")
    host = await booted()
    report = await redact_step(
        host, state_dir=state_dir, run_id=RUN, ordinal=0, reason="x", actor=ACTOR
    )
    assert report.descendants == ("run-2", "run-3")


def flow_host(tmp_path: Path) -> tuple[Host, FlowProvider]:
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    provider = FlowProvider(load_config(toml))
    return Host(provider, LoopbackSingleUserPolicy()), provider


async def test_flow_redaction_request_spool(tmp_path: Path) -> None:
    host, provider = flow_host(tmp_path)
    state_dir = provider.config.state_dir
    write_journal(state_dir)
    request = queue_request(state_dir, RUN, 0, "leak")
    bad = request.with_name("0-bad" + REQUEST_SUFFIX)
    bad.write_text("not json", encoding="utf-8")
    results = await process_requests(host, provider)
    assert len(results) == 2
    assert not request.exists()
    assert not bad.exists()
    ok = json.loads(result_path(request).read_text(encoding="utf-8"))
    assert ok["ok"] is True
    assert ok["runId"] == RUN
    assert json.loads(result_path(bad).read_text(encoding="utf-8"))["ok"] is False


async def test_flow_redaction_poller_only_for_flow_hosts(tmp_path: Path) -> None:
    assert start_poller(Host(EchoProvider(), LoopbackSingleUserPolicy())) is None
    host, _ = flow_host(tmp_path)
    task = start_poller(host)
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def record(self, event: Any) -> None:
        self.events.append(event)


async def test_flow_redaction_audit_names_the_actor(tmp_path: Path) -> None:
    sink = RecordingAudit()
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), audit=sink)
    await host.sequencer.register_channel(MAIN, main_state(), "chat")
    await host.sequencer.register_channel(WORKER, worker_state(), "chat")
    write_journal(tmp_path / "state")
    await redact_step(
        host,
        state_dir=tmp_path / "state",
        run_id=RUN,
        ordinal=0,
        reason="leak",
        actor="flow redact (local operator)",
    )
    events = [e for e in sink.events if e.kind == "chat.redacted"]
    assert len(events) == 2  # the worker part and the main chat's tool call
    assert {e.detail["actor"] for e in events} == {"flow redact (local operator)"}
    assert SECRET not in repr(sink.events)


async def test_flow_redaction_refuses_a_journal_a_live_writer_holds(tmp_path: Path) -> None:
    host = await booted()
    state_dir = tmp_path / "state"
    writer = JournalWriter.create(
        state_dir, RunHeader(RUN, "<library>", None, False, None, 16, None, None)
    )
    writer.append_agent(AgentRecord(0, "k", "ok", TEXT))
    journal = state_dir / "runs" / RUN / "journal.jsonl"
    before = journal.read_text(encoding="utf-8")
    try:
        # Not in live_runs (another process, say): the lock alone refuses it.
        with pytest.raises(RedactionError, match="still being written"):
            await redact_step(
                host, state_dir=state_dir, run_id=RUN, ordinal=0, reason="leak", actor=ACTOR
            )
        assert journal.read_text(encoding="utf-8") == before
        for uri in (MAIN, WORKER):  # nothing of the shared view was touched either
            assert SECRET in json.dumps(host.sequencer.state_of(uri))
    finally:
        writer.close()
    await redact_step(host, state_dir=state_dir, run_id=RUN, ordinal=0, reason="leak", actor=ACTOR)
    assert SECRET not in journal.read_text(encoding="utf-8")


async def test_flow_redaction_partial_failure_reports_and_retry_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = await booted()
    state_dir = tmp_path / "state"
    journal = write_journal(state_dir)
    real = host.redact

    async def flaky(channel: str, *args: Any, **kwargs: Any) -> Any:
        if channel == MAIN:
            raise HostRedactionError("the store is unwritable")
        return await real(channel, *args, **kwargs)

    monkeypatch.setattr(host, "redact", flaky)
    with pytest.raises(RedactionError, match="again") as raised:
        await redact_step(
            host, state_dir=state_dir, run_id=RUN, ordinal=0, reason="leak", actor=ACTOR
        )
    message = str(raised.value)
    assert "unwritable" in message
    assert any("journal" in d for d in raised.value.done)
    assert any(WORKER in d and "p1" in d for d in raised.value.done)
    assert SECRET not in message
    # What was reported is what happened.
    assert SECRET not in journal.read_text(encoding="utf-8")
    assert SECRET not in json.dumps(host.sequencer.state_of(WORKER))
    assert SECRET in json.dumps(host.sequencer.state_of(MAIN))

    monkeypatch.undo()
    report = await redact_step(
        host, state_dir=state_dir, run_id=RUN, ordinal=0, reason="leak", actor=ACTOR
    )
    assert report.worker_parts == 0  # already marked, skipped
    assert SECRET not in json.dumps(host.sequencer.state_of(MAIN))
    assert flow.run_view(host.sequencer.state_of(MAIN)).steps[0].result == "[redacted]"


async def test_flow_redaction_request_spool_records_the_requesting_actor(tmp_path: Path) -> None:
    sink = RecordingAudit()
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    provider = FlowProvider(load_config(toml))
    host = Host(provider, LoopbackSingleUserPolicy(), audit=sink)
    await host.sequencer.register_channel(MAIN, main_state(), "chat")
    await host.sequencer.register_channel(WORKER, worker_state(), "chat")
    write_journal(provider.config.state_dir)
    queue_request(provider.config.state_dir, RUN, 0, "leak", actor="flow redact (alice)")
    await process_requests(host, provider)
    events = [e for e in sink.events if e.kind == "chat.redacted"]
    assert events
    assert {e.detail["actor"] for e in events} == {"flow redact (alice)"}
