"""In-flight records a continued run can never resume are not carried into its resume state.

Only a cut-off call's start record hands a worker chat back to the dispatcher (a reattach
handle), so a restored record no unfinished start of the continued journal names would ride
along for the whole run, and through every restart after it.
"""

from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import JournalWriter, RunHeader, StartRecord
from ahp_host_flow.provider import RunEntry
from tests.unit.test_flow_resume_state import _resumed, _run, _Sink


def _record() -> dict[str, Any]:
    return {"provider": "rh", "turnId": "w-t1", "innerState": {"memory": ["one"]}}


async def test_durinflight_unclaimed_records_are_dropped_on_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = JournalWriter.create(
        tmp_path / "state",
        RunHeader(
            run_id="abc",
            filename="s.py",
            source="return 1",
            has_args=False,
            args=None,
            max_agents=1000,
            budget_total=None,
        ),
    )
    journal.append_started(
        StartRecord(0, "k0", provider="rh", chat="ahp-chat:/w1", worker_chat="ahp-chat:/w1")
    )
    journal.close()
    run = {**_run(), "inflight": {"ahp-chat:/w1": _record(), "ahp-chat:/stale": _record()}}
    _, session = await _resumed(tmp_path, {"flow": 1, "runs": [run]})
    handed: list[RunEntry] = []

    async def fake_run_workflow(**kwargs: Any) -> None:
        handed.append(kwargs["resumed"])

    monkeypatch.setattr(session, "_run_workflow", fake_run_workflow)
    await session.resume_turn("ahp-chat:/main", "t1", _Sink("t1"))  # type: ignore[arg-type]
    (entry,) = handed
    assert entry.durable is not None
    assert set(entry.durable.restored_inflight) == {"ahp-chat:/w1"}
    assert set(entry.durable.inflight) == {"ahp-chat:/w1"}
