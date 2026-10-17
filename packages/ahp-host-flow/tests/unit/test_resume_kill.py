"""Ordinal determinism under latency, and resume after a kill mid-run."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import AgentCall, AgentRecord, RunOptions, call_key, load_journal
from ahp_host_flow.sandbox import run_script

from ..fakes import FakeDispatcher  # noqa: TID252

pytestmark = pytest.mark.node


def _options(tmp_path: Path, **kwargs: Any) -> RunOptions:
    return RunOptions(concurrency=8, timeout=60, state_dir=tmp_path / "state", **kwargs)


async def _run(
    source: str | None,
    node: str,
    dispatcher: FakeDispatcher,
    options: RunOptions,
    logs: list[str] | None = None,
) -> Any:
    return await asyncio.wait_for(
        run_script(
            source,
            node=node,
            dispatcher=dispatcher,
            options=options,
            on_log=logs.append if logs is not None else None,
        ),
        60,
    )


def _records(state_dir: Path, run_id: str) -> list[AgentRecord]:
    return list(load_journal(state_dir, run_id).agents)


def _file_order(state_dir: Path, run_id: str) -> list[int]:
    path = state_dir / "runs" / run_id / "journal.jsonl"
    out: list[int] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("type") == "agent":
            out.append(rec["ordinal"])
    return out


PARALLEL_PIPELINE = (
    "const fan = await parallel([0, 1, 2, 3].map((i) =>"
    " () => agent('fan ' + i, { label: 'fan-' + i })));"
    " const piped = await pipeline(['a', 'b', 'c', 'd'],"
    " (item) => agent('pipe ' + item, { phase: 'Pipe' }),"
    " (prev) => prev.toUpperCase());"
    " return { fan, piped }"
)


def _delays(order: list[int]) -> Mapping[str, float]:
    out: dict[str, float] = {}
    for i in range(4):
        out[f"fan {i}"] = 0.05 * order[i]
        out[f"pipe {'abcd'[i]}"] = 0.05 * order[i]
    return out


async def test_ordinal_determinism_parallel_pipeline(node_path: str, tmp_path: Path) -> None:
    results = []
    for run_id, order in (("a", [0, 1, 2, 3]), ("b", [3, 2, 1, 0])):
        results.append(
            await _run(
                PARALLEL_PIPELINE,
                node_path,
                FakeDispatcher(delays=_delays(order)),
                _options(tmp_path, run_id=run_id),
            )
        )
    state = tmp_path / "state"
    expected = [
        (i, call_key(AgentCall(i, f"fan {i}", {"label": f"fan-{i}"}, None, f"fan-{i}")))
        for i in range(4)
    ] + [
        (4 + i, call_key(AgentCall(4 + i, f"pipe {x}", {"phase": "Pipe"}, "Pipe", None)))
        for i, x in enumerate("abcd")
    ]
    seqs = []
    for run_id in ("a", "b"):
        recs = sorted(_records(state, run_id), key=lambda r: r.ordinal)
        seqs.append([(r.ordinal, r.key) for r in recs])
    assert seqs[0] == seqs[1]
    assert seqs[0] == expected
    assert results[0] == results[1]
    assert _file_order(state, "a") != _file_order(state, "b")


async def test_later_pipeline_stage_replays_despite_completion_order(
    node_path: str, tmp_path: Path
) -> None:
    # A later stage's ordinals follow completion order, which differs on resume
    # (cached calls finish at once); matching by key still replays every call.
    source = (
        "return await pipeline(['a', 'b', 'c'],"
        " (it) => agent('one ' + it), (prev) => agent('two ' + prev))"
    )
    delays = {"one a": 0.1, "one b": 0.05, "one c": 0.0}
    await _run(source, node_path, FakeDispatcher(delays=delays), _options(tmp_path, run_id="first"))
    state = tmp_path / "state"
    by_ordinal = {r.ordinal: r.key for r in _records(state, "first")}
    for ordinal, prompt in ((3, "two one c"), (4, "two one b"), (5, "two one a")):
        assert by_ordinal[ordinal] == call_key(AgentCall(ordinal, prompt))
    dispatcher = FakeDispatcher()
    logs: list[str] = []
    result = await _run(
        None, node_path, dispatcher, _options(tmp_path, run_id="second", resume_from="first"), logs
    )
    assert result == ["two one a", "two one b", "two one c"]
    assert dispatcher.calls == []
    assert not any("cache ended" in line for line in logs)
    ordinals = sorted(r.ordinal for r in _records(state, "second"))
    assert ordinals == [0, 1, 2, 3, 4, 5]


KILL_SCRIPT = (
    "phase('Plan'); const a = await agent('a'); const b = await agent('b');"
    " const c = await agent('c'); phase('Build');"
    " const rest = await parallel([() => agent('hang d'), () => agent('hang e')]);"
    " return { a, b, c, rest }"
)


async def test_resume_kill_mid_run(node_path: str, tmp_path: Path) -> None:
    state = tmp_path / "state"
    reference = await _run(
        KILL_SCRIPT, node_path, FakeDispatcher(hang=False), _options(tmp_path, run_id="ref")
    )
    killed = FakeDispatcher()
    task = asyncio.create_task(
        run_script(
            KILL_SCRIPT,
            node=node_path,
            dispatcher=killed,
            options=_options(tmp_path, run_id="first"),
        )
    )
    await asyncio.wait_for(
        asyncio.gather(killed.wait_started("hang d"), killed.wait_started("hang e")), 10
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [r.ordinal for r in _records(state, "first")] == [0, 1, 2]

    dispatcher = FakeDispatcher(hang=False)
    logs: list[str] = []
    result = await _run(
        None,
        node_path,
        dispatcher,
        _options(tmp_path, run_id="second", resume_from="first"),
        logs,
    )
    assert result == reference
    assert sorted(c.prompt for c in dispatcher.calls) == ["hang d", "hang e"]
    assert (
        "resume: cache ended at agent() #3 (no cached call matches); "
        "3 calls replayed, this call and later ones run live"
    ) in logs
    assert [r.ordinal for r in _records(state, "second")] == [0, 1, 2, 3, 4]


async def test_resume_kill_gap_goes_live(node_path: str, tmp_path: Path) -> None:
    source = (
        "const [x, y] = await parallel([() => agent('hang x'), () => agent('y')]);"
        " const z = await agent('z'); return [x, y, z]"
    )
    state = tmp_path / "state"
    killed = FakeDispatcher()
    task = asyncio.create_task(
        run_script(
            source,
            node=node_path,
            dispatcher=killed,
            options=_options(tmp_path, run_id="first"),
        )
    )
    await asyncio.wait_for(killed.wait_started("hang x"), 10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if any(r.ordinal == 1 for r in _records(state, "first")):
                break
        except OSError:
            pass
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    dispatcher = FakeDispatcher(hang=False)
    logs: list[str] = []
    result = await _run(
        None,
        node_path,
        dispatcher,
        _options(tmp_path, run_id="second", resume_from="first"),
        logs,
    )
    assert result == ["hang x", "y", "z"]
    assert sorted(c.prompt for c in dispatcher.calls) == ["hang x", "y", "z"]
    assert any("#0 (no cached call matches)" in line for line in logs)
