"""Editing the script between runs: the longest unchanged prefix stays cached."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import RunOptions, load_journal
from ahp_host_flow.sandbox import run_script

from ..fakes import FakeDispatcher  # noqa: TID252

pytestmark = pytest.mark.node

V1 = (
    "phase('One'); const a = await agent('a');"
    " const b = await agent('b', { label: 'B' });"
    " phase('Two'); const c = await agent('c'); const d = await agent('d');"
    " return [a, b, c, d]"
)


def _options(tmp_path: Path, **kwargs: Any) -> RunOptions:
    return RunOptions(concurrency=4, timeout=60, state_dir=tmp_path / "state", **kwargs)


async def _run(
    source: str,
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


async def _resume_edit(
    node: str, tmp_path: Path, source: str, logs: list[str] | None = None
) -> tuple[Any, list[str]]:
    await _run(V1, node, FakeDispatcher(), _options(tmp_path, run_id="r1"))
    dispatcher = FakeDispatcher()
    result = await _run(
        source,
        node,
        dispatcher,
        _options(tmp_path, run_id="r2", resume_from="r1"),
        logs,
    )
    return result, [c.prompt for c in dispatcher.calls]


async def test_resume_edit_prompt(node_path: str, tmp_path: Path) -> None:
    v2 = V1.replace("agent('c')", "agent('c2')")
    logs: list[str] = []
    result, dispatched = await _resume_edit(node_path, tmp_path, v2, logs)
    assert dispatched == ["c2", "d"]
    assert result == ["a", "b", "c2", "d"]
    assert any(
        "resume: cache ended at agent() #2 (no cached call matches); "
        "2 calls replayed, this call and later ones run live" in line
        for line in logs
    )
    assert load_journal(tmp_path / "state", "r2").header.source == v2


async def test_resume_edit_label(node_path: str, tmp_path: Path) -> None:
    _, dispatched = await _resume_edit(node_path, tmp_path, V1.replace("'B'", "'B2'"))
    assert dispatched == ["b", "c", "d"]


async def test_resume_edit_phase(node_path: str, tmp_path: Path) -> None:
    _, dispatched = await _resume_edit(node_path, tmp_path, V1.replace("'Two'", "'Three'"))
    assert dispatched == ["c", "d"]


async def test_resume_edit_insert_first(node_path: str, tmp_path: Path) -> None:
    v2 = "await agent('z'); " + V1
    _, dispatched = await _resume_edit(node_path, tmp_path, v2)
    assert dispatched == ["z", "a", "b", "c", "d"]


async def test_resume_edit_unchanged_tail_after_edit_still_live(
    node_path: str, tmp_path: Path
) -> None:
    v2 = V1.replace("agent('a')", "agent('a2')")
    _, dispatched = await _resume_edit(node_path, tmp_path, v2)
    assert dispatched == ["a2", "b", "c", "d"]
