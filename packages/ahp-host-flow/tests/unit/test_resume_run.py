"""Journaled runs through the sandbox: write, replay, chain and resume from snapshots."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import (
    JournalError,
    JournalRun,
    RunOptions,
    call_key,
    load_journal,
    run_dir,
)
from ahp_host_flow.sandbox import run_script

from ..fakes import FakeDispatcher  # noqa: TID252

pytestmark = pytest.mark.node

THREE = "return [await agent('a'), await agent('b'), await agent('c')]"


def _options(tmp_path: Path, **kwargs: Any) -> RunOptions:
    return RunOptions(timeout=60, concurrency=4, state_dir=tmp_path / "state", **kwargs)


async def _run(
    source: str | None, node: str, dispatcher: FakeDispatcher, options: RunOptions, **kwargs: Any
) -> Any:
    return await asyncio.wait_for(
        run_script(source, node=node, dispatcher=dispatcher, options=options, **kwargs), 60
    )


def _shape(tmp_path: Path, run_id: str) -> list[tuple[int, str, str, Any]]:
    journal = load_journal(tmp_path / "state", run_id)
    return [(r.ordinal, r.key, r.status, r.value) for r in journal.agents]


async def test_resume_run_writes_journal(node_path: str, tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    started: list[str] = []
    result = await _run(
        THREE,
        node_path,
        dispatcher,
        _options(tmp_path, run_id="r1"),
        filename="three.js",
        on_run_start=started.append,
    )
    assert result == ["a", "b", "c"]
    assert started == ["r1"]
    journal = load_journal(tmp_path / "state", "r1")
    assert journal.header.source == THREE
    assert journal.header.filename == "three.js"
    assert [r.ordinal for r in journal.agents] == [0, 1, 2]
    assert [r.status for r in journal.agents] == ["ok"] * 3
    assert [r.key for r in journal.agents] == [call_key(c) for c in dispatcher.calls]


async def test_resume_run_generated_id(node_path: str, tmp_path: Path) -> None:
    started: list[str] = []
    options = _options(tmp_path)
    await _run(THREE, node_path, FakeDispatcher(), options, on_run_start=started.append)
    assert len(started) == 1
    assert re.fullmatch(r"[0-9a-f]{32}", started[0])
    assert run_dir(tmp_path / "state", started[0]).is_dir()


async def test_resume_run_replays_all(node_path: str, tmp_path: Path) -> None:
    first = await _run(THREE, node_path, FakeDispatcher(), _options(tmp_path, run_id="r1"))
    dispatcher = FakeDispatcher()
    second = await _run(
        None, node_path, dispatcher, _options(tmp_path, run_id="r2", resume_from="r1")
    )
    assert second == first
    assert dispatcher.calls == []
    assert load_journal(tmp_path / "state", "r2").header.resumed_from == "r1"
    assert _shape(tmp_path, "r2") == _shape(tmp_path, "r1")


async def test_resume_run_chain(node_path: str, tmp_path: Path) -> None:
    first = await _run(THREE, node_path, FakeDispatcher(), _options(tmp_path, run_id="r1"))
    await _run(None, node_path, FakeDispatcher(), _options(tmp_path, run_id="r2", resume_from="r1"))
    dispatcher = FakeDispatcher()
    third = await _run(
        None, node_path, dispatcher, _options(tmp_path, run_id="r3", resume_from="r2")
    )
    assert third == first
    assert dispatcher.calls == []


async def test_resume_run_missing_snapshot(node_path: str, tmp_path: Path) -> None:
    d = run_dir(tmp_path / "state", "r1")
    d.mkdir(parents=True)
    header = {
        "type": "run",
        "version": 1,
        "runId": "r1",
        "resumedFrom": None,
        "filename": "w.js",
        "hasArgs": False,
        "args": None,
        "options": {"maxAgents": 1000, "budgetTotal": None},
    }
    (d / "journal.jsonl").write_text(json.dumps(header) + "\n", encoding="utf-8")
    dispatcher = FakeDispatcher()
    with pytest.raises(JournalError, match="no script snapshot"):
        await _run(None, node_path, dispatcher, _options(tmp_path, run_id="r2", resume_from="r1"))
    assert dispatcher.calls == []


async def test_resume_run_no_state_dir(node_path: str) -> None:
    with pytest.raises(ValueError, match="a script source is needed"):
        await _run(None, node_path, FakeDispatcher(), RunOptions(timeout=60))


async def test_resume_run_child_from_snapshot(node_path: str, tmp_path: Path) -> None:
    wf = tmp_path / "wf"
    wf.mkdir()
    child = wf / "child.js"
    child.write_text(
        "export const meta = { name: 'child', description: 'c' }\n"
        "return { reply: await agent('child ' + args.who), args }",
        encoding="utf-8",
    )
    source = (
        "return [await workflow('child', {who: 'n'}),"
        " await workflow({scriptPath: 'wf/child.js'}, {who: 'p'})]"
    )
    extra: dict[str, Any] = {"run_root": tmp_path, "workflow_dirs": (wf,)}
    first = await _run(
        source, node_path, FakeDispatcher(), _options(tmp_path, run_id="r1", **extra)
    )
    child.unlink()
    dispatcher = FakeDispatcher()
    second = await _run(
        None,
        node_path,
        dispatcher,
        _options(tmp_path, run_id="r2", resume_from="r1", **extra),
    )
    assert second == first
    assert dispatcher.calls == []
    assert len(load_journal(tmp_path / "state", "r2").workflows) == 2


async def test_resume_run_budget_identical(node_path: str, tmp_path: Path) -> None:
    source = (
        "const out = []; try { for (let i = 0; i < 10; i++) out.push(await agent('b' + i)) }"
        " catch (e) { out.push(e.message) } return { out, spent: budget.spent() }"
    )
    expected = {
        "out": ["b0", "b1", "b2", "budget exhausted: 3 of 3 output tokens spent"],
        "spent": 3,
    }
    first = await _run(
        source,
        node_path,
        FakeDispatcher(output_tokens=1),
        _options(tmp_path, run_id="r1", budget_total=3),
    )
    assert first == expected
    dispatcher = FakeDispatcher(output_tokens=1)
    second = await _run(
        None,
        node_path,
        dispatcher,
        _options(tmp_path, run_id="r2", resume_from="r1", budget_total=None),
    )
    assert second == expected
    assert dispatcher.calls == []


async def test_resume_run_cap_identical(node_path: str, tmp_path: Path) -> None:
    source = (
        "const out = []; try { for (let i = 0; i < 5; i++) out.push(await agent('c' + i)) }"
        " catch (e) { out.push(e.message) } return out"
    )
    expected = ["c0", "c1", "agent() limit reached: at most 2 agent() calls per run"]
    first = await _run(
        source, node_path, FakeDispatcher(), _options(tmp_path, run_id="r1", max_agents=2)
    )
    assert first == expected
    dispatcher = FakeDispatcher()
    second = await _run(
        None, node_path, dispatcher, _options(tmp_path, run_id="r2", resume_from="r1")
    )
    assert second == expected
    assert dispatcher.calls == []


async def test_resume_run_args_from_header(node_path: str, tmp_path: Path) -> None:
    source = "return [args, await agent('x')]"
    first = await _run(
        source,
        node_path,
        FakeDispatcher(),
        _options(tmp_path, run_id="r1"),
        args={"who": "x"},
    )
    assert first == [{"who": "x"}, "x"]
    with pytest.raises(ValueError, match="args come from the journal"):
        await _run(
            None,
            node_path,
            FakeDispatcher(),
            _options(tmp_path, run_id="r2", resume_from="r1"),
            args={"who": "y"},
        )
    second = await _run(
        None, node_path, FakeDispatcher(), _options(tmp_path, run_id="r3", resume_from="r1")
    )
    assert second == first


async def test_resume_run_statuses_replayed(node_path: str, tmp_path: Path) -> None:
    source = "return [await agent('fail'), await agent('skip')]"
    first = await _run(source, node_path, FakeDispatcher(), _options(tmp_path, run_id="r1"))
    assert first == [None, None]
    dispatcher = FakeDispatcher()
    second = await _run(
        None, node_path, dispatcher, _options(tmp_path, run_id="r2", resume_from="r1")
    )
    assert second == [None, None]
    assert dispatcher.calls == []


async def test_resume_run_carries_snapshots_it_never_reached(
    node_path: str, tmp_path: Path
) -> None:
    # r2 resumes r1 and stops before reaching workflow(); r3 resumes r2 after the
    # child file is gone, so r2's journal must already hold r1's child snapshot.
    wf = tmp_path / "wf"
    wf.mkdir()
    child = wf / "child.js"
    child.write_text("return await agent('child ' + args.who)", encoding="utf-8")
    source = "return await workflow('child', {who: 'n'})"
    extra: dict[str, Any] = {"run_root": tmp_path, "workflow_dirs": (wf,)}
    first = await _run(
        source, node_path, FakeDispatcher(), _options(tmp_path, run_id="r1", **extra)
    )
    JournalRun.open(
        _options(tmp_path, run_id="r2", resume_from="r1", **extra),
        source=None,
        filename="workflow.js",
        has_args=False,
        args=None,
    ).close()
    assert len(load_journal(tmp_path / "state", "r2").workflows) == 1
    child.unlink()
    dispatcher = FakeDispatcher()
    third = await _run(
        None, node_path, dispatcher, _options(tmp_path, run_id="r3", resume_from="r2", **extra)
    )
    assert third == first
    assert [c.prompt for c in dispatcher.calls] == ["child n"]


async def test_resume_run_unwritable_header_leaves_no_run(node_path: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        await _run(
            "return 1",
            node_path,
            FakeDispatcher(),
            _options(tmp_path, run_id="nan"),
            args=float("nan"),
        )
    assert not run_dir(tmp_path / "state", "nan").exists()
