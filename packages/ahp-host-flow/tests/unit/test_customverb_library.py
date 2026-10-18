"""Custom verbs through the library API: `run(verbs=...)` and `FlowRun.verb`. No Node."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow import library
from ahp_host_flow.engine import RunOptions, VerbFailedError
from tests.hook_fakes import FakeVerbs, counting_verb, echo_verb

from ..fakes import FakeDispatcher  # noqa: TID252


async def test_customverb_library_calls() -> None:
    fakes = FakeVerbs({"tool": "echo", "boom": "fail"})
    async with library.run(FakeDispatcher(), verbs=fakes.handlers) as r:
        assert await r.verb("tool", "x") == {"name": "tool", "args": ["x"]}
        with pytest.raises(ValueError, match="unknown verb"):
            await r.verb("nope")
        with pytest.raises(VerbFailedError) as info:
            await r.verb("boom", 1)
    assert str(info.value) == "boom failed: 1"


async def test_customverb_library_reserved() -> None:
    with pytest.raises(ValueError, match="reserved"):
        async with library.run(FakeDispatcher(), verbs={"phase": echo_verb}):
            pass


async def test_customverb_library_context_has_no_gates() -> None:
    fakes = FakeVerbs({"asker": "ask"})
    async with library.run(FakeDispatcher(), verbs=fakes.handlers) as r:
        with pytest.raises(VerbFailedError) as info:
            await r.verb("asker")
    assert str(info.value).startswith("ask() has no answer path")


async def test_customverb_library_journal_resume(tmp_path: Path) -> None:
    calls: list[str] = []
    handlers = {"tool": counting_verb(calls)}
    options = RunOptions(state_dir=tmp_path, run_id="r1")
    async with library.run(FakeDispatcher(), options, verbs=handlers) as r:
        first = [await r.verb("tool", "a"), await r.verb("tool", "b")]
    assert len(calls) == 2
    options = RunOptions(state_dir=tmp_path, run_id="r2", resume_from="r1")
    async with library.run(FakeDispatcher(), options, verbs=handlers) as r:
        again = [await r.verb("tool", "a"), await r.verb("tool", "b")]
    assert again == first
    assert len(calls) == 2
    journal = tmp_path / "runs" / "r2" / "journal.jsonl"
    records: list[dict[str, Any]] = [
        json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
    ]
    verbs = [rec for rec in records if rec.get("type") == "verb"]
    assert verbs
    assert {rec["status"] for rec in verbs} == {"done"}


async def test_customverb_library_child_workflow(tmp_path: Path) -> None:
    (tmp_path / "child.py").write_text(
        "async def main(args):\n    return await tool('c')\n", encoding="utf-8"
    )
    fakes = FakeVerbs({"tool": "echo"})
    options = RunOptions(run_root=tmp_path)
    async with library.run(FakeDispatcher(), options, verbs=fakes.handlers) as r:
        await r.verb("tool", "p")
        result = await asyncio.wait_for(r.workflow({"scriptPath": "child.py"}), 60)
    assert result == {"name": "tool", "args": ["c"]}
    assert [(c.ordinal, c.args) for c in fakes.calls] == [(0, ("p",)), (1, ("c",))]
