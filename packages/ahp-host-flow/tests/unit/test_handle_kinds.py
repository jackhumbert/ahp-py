"""A dispatch handle is read only by the dispatcher kind that wrote it (plan §8 M17 item 3).

A fleet or remote call's handle names a `session`; an in-process call's names a `worker_chat`.
A handle of the other kind (the provider changed kind across a restart, say) is ignored, so a
cut-off call never reattaches to, or stops, something another kind of dispatcher left.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ahp_host_flow.dispatch import DurableWorkers, ProviderDispatcher
from ahp_host_flow.engine import (
    AgentCall,
    ClientDispatcher,
    DispatchHandle,
    JournalWriter,
    RemoteSpec,
    RunHeader,
    StartRecord,
    load_journal,
    run_dir,
)
from ahp_host_flow.engine.dispatcher import _REATTACH
from ahp_host_flow.fleet import reattach_session
from tests.unit.test_remote_dispatch_engine import Far

IN_PROCESS = DispatchHandle("p", "chat:/w1", worker_chat="chat:/w1")
FLEET = DispatchHandle("p", "chat:/w1", session="claude:/s1")


def test_handle_kinds_start_record_carries_the_worker_chat(tmp_path: Path) -> None:
    writer = JournalWriter.create(tmp_path, RunHeader("r1", "w.js", "x", False, None, 10, None))
    writer.append_started(StartRecord(0, "k0", "p", "chat:/w1", worker_chat="chat:/w1"))
    writer.close()
    loaded = load_journal(tmp_path, "r1")
    assert loaded.started == (StartRecord(0, "k0", "p", "chat:/w1", None, "chat:/w1"),)
    line = (run_dir(tmp_path, "r1") / "journal.jsonl").read_text("utf-8").splitlines()[1]
    assert json.loads(line)["handle"] == {
        "provider": "p",
        "chat": "chat:/w1",
        "workerChat": "chat:/w1",
    }


def test_handle_kinds_fleet_ignores_an_in_process_handle() -> None:
    assert reattach_session(IN_PROCESS, "p") is None
    assert reattach_session(FLEET, "p") == "claude:/s1"
    assert reattach_session(FLEET, "other") is None


async def test_handle_kinds_remote_ignores_an_in_process_handle() -> None:
    far = Far()
    lines: list[str] = []
    dispatcher = ClientDispatcher(
        RemoteSpec("p", "claude", transport_factory=far.dial), on_log=lines.append
    )
    token = _REATTACH.set(IN_PROCESS)
    try:
        call = asyncio.create_task(dispatcher.run_agent(AgentCall(0, "hi")))
    finally:
        _REATTACH.reset(token)
    try:
        result = await asyncio.wait_for(call, 10)
        assert result.value == "You said: hi"
        # No cut-off remote turn was looked for: the handle names no session.
        assert lines == []
    finally:
        await dispatcher.aclose()
        for task in list(far.serving):
            task.cancel()


async def test_handle_kinds_in_process_ignores_a_session_handle() -> None:
    record = {"provider": "p", "turnId": "t", "innerState": {}}
    durable = DurableWorkers(
        inflight={"chat:/w1": dict(record)}, restored_inflight={"chat:/w1": dict(record)}
    )
    dispatcher = ProviderDispatcher.__new__(ProviderDispatcher)
    dispatcher.durable = durable
    call = AgentCall(0, "hi")
    resumed = await dispatcher._resume_inflight(call, "p", None, None, {}, None, FLEET)  # type: ignore[arg-type]
    assert resumed is None
    # Nothing consumed: the in-process record is still there for its own handle.
    assert durable.restored_inflight == {"chat:/w1": record}
    assert durable.inflight == {"chat:/w1": record}
