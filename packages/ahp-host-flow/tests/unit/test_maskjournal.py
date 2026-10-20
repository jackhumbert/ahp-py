"""A masked journal: script, args and child sources stored masked, resumed by their sha256."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import (
    EchoDispatcher,
    JournalError,
    Redactor,
    RunOptions,
    args_masked,
    canonical_json,
    load_journal,
    source_masked,
    text_sha,
)
from ahp_host_flow.engine.journal import value_sha
from ahp_host_flow.sandbox import ScriptError, run_script
from tests.flow_fakes import AWS, GH

SOURCE = f"# token = \"{GH}\"\nasync def main(args):\n    return await agent(args['k'])\n"
ARGS = {"k": "hi", "key": AWS}
CHILD = f'# token = "{GH}"\nasync def main(args):\n    return await agent("kid")\n'
PARENT = "async def main(args):\n    return await workflow('child')\n"


def _journal_text(tmp_path: Path, run_id: str) -> str:
    return (tmp_path / "state" / "runs" / run_id / "journal.jsonl").read_text(encoding="utf-8")


def _options(tmp_path: Path, run_id: str, **kwargs: Any) -> RunOptions:
    kwargs.setdefault("redact", Redactor().mask_value)
    return RunOptions(state_dir=tmp_path / "state", run_id=run_id, **kwargs)


async def _run(
    source: str | None, dispatcher: EchoDispatcher, options: RunOptions, **kwargs: Any
) -> Any:
    kwargs.setdefault("filename", "w.py")
    return await asyncio.wait_for(
        run_script(source, node=None, dispatcher=dispatcher, options=options, **kwargs), 30
    )


async def _first(tmp_path: Path, **kwargs: Any) -> None:
    result = await _run(SOURCE, EchoDispatcher(), _options(tmp_path, "r1", **kwargs), args=ARGS)
    assert result == "hi"


async def test_maskjournal_header_and_args_masked(tmp_path: Path) -> None:
    await _first(tmp_path)
    text = _journal_text(tmp_path, "r1")
    assert GH not in text
    assert AWS not in text
    header = json.loads(text.split("\n")[0])
    assert header["sourceSha"] == text_sha(SOURCE)
    assert header["argsSha"] == value_sha(ARGS)
    loaded = load_journal(tmp_path / "state", "r1").header
    assert source_masked(loaded) is True
    assert args_masked(loaded) is True


async def test_maskjournal_resumes_same_script(tmp_path: Path) -> None:
    await _first(tmp_path)
    dispatcher = EchoDispatcher()
    options = _options(tmp_path, "r2", resume_from="r1")
    assert await _run(SOURCE, dispatcher, options, args=ARGS) == "hi"
    assert dispatcher.calls == []


async def test_maskjournal_refuses_other_script_and_args(tmp_path: Path) -> None:
    await _first(tmp_path)

    async def resume(run_id: str, source: str | None, **kwargs: Any) -> None:
        options = _options(tmp_path, run_id, resume_from="r1")
        await _run(source, EchoDispatcher(), options, **kwargs)

    with pytest.raises(JournalError) as info:
        await resume("r2", SOURCE + "# edited\n", args=ARGS)
    assert "the script differs" in str(info.value)
    with pytest.raises(JournalError) as info:
        await resume("r3", None, args=ARGS)
    assert "holds a masked copy" in str(info.value)
    with pytest.raises(JournalError) as info:
        await resume("r4", SOURCE)
    assert "holds masked args" in str(info.value)
    with pytest.raises(JournalError) as info:
        await resume("r5", SOURCE, args={"k": "hi", "key": "other"})
    assert "the args differ" in str(info.value)


async def test_maskjournal_keep_source_keeps_text(tmp_path: Path) -> None:
    await _first(tmp_path, keep_source=True)
    text = _journal_text(tmp_path, "r1")
    assert GH in text
    assert AWS in text
    header = load_journal(tmp_path / "state", "r1").header
    assert header.source == SOURCE
    assert header.args == ARGS
    assert header.source_sha == text_sha(SOURCE)
    assert header.args_sha == value_sha(ARGS)
    assert source_masked(header) is False
    assert args_masked(header) is False
    dispatcher = EchoDispatcher()
    options = _options(tmp_path, "r2", resume_from="r1")
    assert await _run(None, dispatcher, options) == "hi"
    assert dispatcher.calls == []


async def test_maskjournal_unmasked_unchanged(tmp_path: Path) -> None:
    options = RunOptions(state_dir=tmp_path / "state", run_id="r1")
    assert await _run(SOURCE, EchoDispatcher(), options, args=ARGS) == "hi"
    header = load_journal(tmp_path / "state", "r1").header
    assert header.source == SOURCE
    assert header.args == ARGS
    assert header.source_sha == text_sha(SOURCE)
    assert header.args_sha == value_sha(ARGS)
    resumed = RunOptions(state_dir=tmp_path / "state", run_id="r2", resume_from="r1")
    with pytest.raises(ValueError, match="args come from the journal") as info:
        await _run(None, EchoDispatcher(), resumed, args=ARGS)
    assert str(info.value) == "args come from the journal on resume"


async def test_maskjournal_child_workflow(tmp_path: Path) -> None:
    wf = tmp_path / "wf"
    wf.mkdir()
    (wf / "child.py").write_text(CHILD, encoding="utf-8")
    first = _options(tmp_path, "r1", workflow_dirs=(wf,), run_root=tmp_path)
    assert await _run(PARENT, EchoDispatcher(), first) == "kid"
    text = _journal_text(tmp_path, "r1")
    assert GH not in text
    records = [json.loads(line) for line in text.splitlines()]
    child = [r for r in records if r["type"] == "workflow"]
    assert len(child) == 1
    assert GH not in child[0]["source"]
    assert child[0]["sourceSha"] == text_sha(CHILD)
    assert canonical_json(child[0]["ref"]) == canonical_json("child")

    dispatcher = EchoDispatcher()
    second = _options(tmp_path, "r2", resume_from="r1", workflow_dirs=(wf,), run_root=tmp_path)
    assert await _run(PARENT, dispatcher, second) == "kid"
    assert dispatcher.calls == []

    (wf / "child.py").write_text(CHILD + "# edited\n", encoding="utf-8")
    third = _options(tmp_path, "r3", resume_from="r2", workflow_dirs=(wf,), run_root=tmp_path)
    with pytest.raises(ScriptError) as info:
        await _run(PARENT, EchoDispatcher(), third)
    assert "the journal holds a masked copy and the current script differs" in str(info.value)


async def test_maskjournal_resume_from_options(tmp_path: Path) -> None:
    """A library caller hands the real script and args on `RunOptions`, checked by sha256."""
    await _first(tmp_path)
    dispatcher = EchoDispatcher()
    options = _options(
        tmp_path, "r2", resume_from="r1", resume_source=SOURCE, resume_args=dict(ARGS)
    )
    assert await _run(None, dispatcher, options) == "hi"
    assert dispatcher.calls == []
    # The secrets stay out of the options' repr, as they stay out of the journal.
    assert GH not in repr(options)
    assert AWS not in repr(options)
    other = _options(
        tmp_path, "r3", resume_from="r1", resume_source=SOURCE, resume_args={"k": "hi"}
    )
    with pytest.raises(JournalError, match="the args differ"):
        await _run(None, EchoDispatcher(), other)
    edited = _options(
        tmp_path, "r4", resume_from="r1", resume_source=SOURCE + "#\n", resume_args=dict(ARGS)
    )
    with pytest.raises(JournalError, match="the script differs"):
        await _run(None, EchoDispatcher(), edited)


async def test_maskjournal_resume_args_on_unmasked_journal_are_checked(tmp_path: Path) -> None:
    options = RunOptions(state_dir=tmp_path / "state", run_id="r1")
    assert await _run(SOURCE, EchoDispatcher(), options, args=ARGS) == "hi"
    same = RunOptions(
        state_dir=tmp_path / "state", run_id="r2", resume_from="r1", resume_args=dict(ARGS)
    )
    assert await _run(None, EchoDispatcher(), same) == "hi"
    other = RunOptions(
        state_dir=tmp_path / "state", run_id="r3", resume_from="r1", resume_args={"k": "x"}
    )
    with pytest.raises(JournalError, match="resume_args differ"):
        await _run(None, EchoDispatcher(), other)


def test_maskjournal_resume_values_need_resume_from(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="need resume_from"):
        RunOptions(resume_source="x")
    with pytest.raises(ValueError, match="need resume_from"):
        RunOptions(resume_args=None)


async def test_maskjournal_log_lines_and_errors_masked(tmp_path: Path) -> None:
    """The sandbox masks a script's log() lines and its error before any caller sees them."""
    script = (
        "async def main(args):\n    log('k=' + args['key'])\n    raise ValueError(args['key'])\n"
    )
    lines: list[str] = []
    options = RunOptions(redact=Redactor().mask_value)
    with pytest.raises(ScriptError) as info:
        await asyncio.wait_for(
            run_script(
                script,
                node=None,
                dispatcher=EchoDispatcher(),
                options=options,
                args=ARGS,
                filename="w.py",
                on_log=lines.append,
            ),
            30,
        )
    assert lines == ["k=[redacted]"]
    assert AWS not in str(info.value)
    assert AWS not in (info.value.stack or "")
    assert AWS not in info.value.stderr


async def test_maskjournal_library_log_masked() -> None:
    from ahp_host_flow.library import run

    lines: list[str] = []
    async with run(
        EchoDispatcher(), RunOptions(redact=Redactor().mask_value), on_log=lines.append
    ) as flow:
        flow.log(f"key {AWS}")
    assert lines == ["key [redacted]"]
