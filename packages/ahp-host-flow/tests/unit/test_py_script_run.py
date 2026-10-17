"""Running `.py` scripts through the Python runner, chosen by file type; no Node needed."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import RunOptions, load_journal
from ahp_host_flow.sandbox import (
    MetaError,
    NodeNotFoundError,
    RunTimeoutError,
    Sandbox,
    SandboxError,
    ScriptError,
    ScriptSyntaxError,
    extract_meta,
    run_script,
)

from ..fakes import FakeDispatcher  # noqa: TID252


async def _run(source: str | None, dispatcher: FakeDispatcher | None = None, **kwargs: Any) -> Any:
    kwargs.setdefault("filename", "w.py")
    return await asyncio.wait_for(
        run_script(source, node=None, dispatcher=dispatcher or FakeDispatcher(), **kwargs), 30
    )


def _ordinals(dispatcher: FakeDispatcher) -> dict[str, int]:
    return {c.prompt: c.ordinal for c in dispatcher.calls}


async def test_py_script_run_returns_value() -> None:
    assert await _run("async def main(args):\n    return 1 + 1\n") == 2


async def test_py_script_run_globals_are_verbs() -> None:
    source = (
        "async def main(a):\n"
        "    phase('P')\n"
        "    log('x')\n"
        "    one = await agent('one')\n"
        "    many = await parallel([lambda: agent('p')])\n"
        "    piped = await pipeline(['q'], lambda prev, item, i: agent(item))\n"
        "    return [one, many, piped, args, a, budget.total]\n"
    )
    result = await _run(source, args={"k": 1})
    assert result == ["one", ["p"], ["q"], {"k": 1}, {"k": 1}, None]


async def test_py_script_run_issue_time() -> None:
    dispatcher = FakeDispatcher()
    source = (
        "async def main(args):\n"
        "    f = agent('a')\n"
        "    g = agent('b')\n"
        "    return [await g, await f]\n"
    )
    assert await _run(source, dispatcher) == ["b", "a"]
    assert _ordinals(dispatcher) == {"a": 0, "b": 1}


async def test_py_script_run_parallel_ordinals() -> None:
    dispatcher = FakeDispatcher()
    source = (
        "async def main(args):\n"
        "    return await parallel([lambda: agent('a'), lambda: agent('b'), lambda: agent('c')])\n"
    )
    assert await _run(source, dispatcher) == ["a", "b", "c"]
    assert _ordinals(dispatcher) == {"a": 0, "b": 1, "c": 2}


async def test_py_script_run_log_phase() -> None:
    dispatcher = FakeDispatcher()
    logs: list[str] = []
    phases: list[str] = []
    source = "async def main(args):\n    phase('P')\n    log('x')\n    return await agent('a')\n"
    await _run(source, dispatcher, on_log=logs.append, on_phase=phases.append)
    assert "x" in logs
    assert phases == ["P"]
    assert [c.phase for c in dispatcher.calls] == ["P"]


async def test_py_script_run_script_error() -> None:
    with pytest.raises(ScriptError) as info:
        await _run("async def main(args):\n    raise ValueError('boom')\n")
    assert str(info.value) == "boom"
    assert info.value.js_name == "ValueError"


async def test_py_script_run_syntax_and_meta() -> None:
    with pytest.raises(ScriptSyntaxError):
        await _run("async def main(args:\n    return 1\n")
    with pytest.raises(MetaError):
        await _run("meta = {'a': f()}\nasync def main(args):\n    return 1\n")


async def test_py_script_run_no_main() -> None:
    with pytest.raises(ScriptError) as info:
        await _run("x = 1\n")
    assert str(info.value) == "a Python workflow needs `async def main(args)`"


async def test_py_script_run_timeout() -> None:
    source = "async def main(args):\n    while True:\n        pass\n"
    with pytest.raises(RunTimeoutError):
        await _run(source, options=RunOptions(timeout=1.0))


async def test_py_script_run_in_process_child(tmp_path: Path) -> None:
    (tmp_path / "c.py").write_text(
        "async def main(args):\n    return [args['who'], await agent('child')]\n",
        encoding="utf-8",
    )
    dispatcher = FakeDispatcher()
    source = (
        "async def main(args):\n"
        "    first = await agent('parent')\n"
        "    return [first, await workflow({'scriptPath': 'c.py'}, {'who': 'x'})]\n"
    )
    result = await _run(source, dispatcher, options=RunOptions(run_root=tmp_path))
    assert result == ["parent", ["x", "child"]]
    assert _ordinals(dispatcher) == {"parent": 0, "child": 1}


async def test_py_script_run_journal_and_resume(tmp_path: Path) -> None:
    source = "async def main(args):\n    return [await agent('a'), await agent('b')]\n"
    state = tmp_path / "state"
    first = FakeDispatcher()
    options = RunOptions(state_dir=state, run_id="first")
    assert await _run(source, first, options=options) == ["a", "b"]
    header = load_journal(state, "first").header
    assert header.filename.endswith(".py")
    assert header.source == source
    second = FakeDispatcher()
    resumed = RunOptions(state_dir=state, run_id="second", resume_from="first")
    assert await _run(None, second, options=resumed) == ["a", "b"]
    assert second.calls == []


CANCELS_ITSELF = (
    "import asyncio\n"
    "async def main(args):\n"
    "    t = asyncio.ensure_future(asyncio.sleep(10))\n"
    "    t.cancel()\n"
    "    await t\n"
)


async def test_py_script_run_cancelled_error_is_a_script_error() -> None:
    # A CancelledError the script raises itself used to end the request task cancelled
    # with no reply, hanging the run.
    with pytest.raises(ScriptError, match="the script was cancelled"):
        await asyncio.wait_for(_run(CANCELS_ITSELF), 10)


async def test_py_script_run_cancelled_error_in_child(tmp_path: Path) -> None:
    (tmp_path / "c.py").write_text(CANCELS_ITSELF, encoding="utf-8")
    source = (
        "async def main(args):\n"
        "    try:\n"
        "        await workflow({'scriptPath': 'c.py'})\n"
        "    except Exception as exc:\n"
        "        caught = str(exc)\n"
        "    return [caught, await agent('after')]\n"
    )
    result = await asyncio.wait_for(_run(source, options=RunOptions(run_root=tmp_path)), 10)
    assert result == ["the script was cancelled", "after"]


async def test_py_script_run_non_finite_floats_are_null() -> None:
    # JSON.stringify gives null for Infinity and NaN; allow_nan=False used to fail the run.
    dispatcher = FakeDispatcher()
    source = (
        "async def main(args):\n"
        "    await agent('a', temperature=float('nan'), levels=[float('-inf'), 1.5])\n"
        "    return {'left': budget.remaining(), 'nan': float('nan'), 'ok': 1.5}\n"
    )
    assert await _run(source, dispatcher) == {"left": None, "nan": None, "ok": 1.5}
    (call,) = dispatcher.calls
    assert call.opts["temperature"] is None
    assert call.opts["levels"] == [None, 1.5]


async def test_py_script_run_non_finite_workflow_args_and_log(tmp_path: Path) -> None:
    (tmp_path / "c.py").write_text(
        "async def main(args):\n    return [args, float('inf')]\n", encoding="utf-8"
    )
    logs: list[str] = []
    source = (
        "async def main(args):\n"
        "    log(float('inf'))\n"
        "    return await workflow({'scriptPath': 'c.py'}, {'x': float('nan')})\n"
    )
    result = await _run(source, options=RunOptions(run_root=tmp_path), on_log=logs.append)
    assert result == [{"x": None}, None]
    assert "Infinity" in logs


async def test_py_script_error_texts_match_the_js_runner() -> None:
    from ahp_host_flow.sandbox.client import RUNNER

    js = RUNNER.read_text(encoding="utf-8")
    source = (
        "async def main(args):\n"
        "    out = []\n"
        "    for call in (lambda: parallel(1), lambda: pipeline(1), lambda: pipeline([], 1)):\n"
        "        try:\n"
        "            await call()\n"
        "        except TypeError as exc:\n"
        "            out.append(str(exc))\n"
        "    return out\n"
    )
    texts = await _run(source)
    assert texts == [
        "parallel() needs an array of functions",
        "pipeline() needs an array of items",
        "pipeline() stages must be functions",
    ]
    for text in texts:
        assert f"'{text}'" in js


async def test_sandbox_language_mismatch_leaves_no_journal(tmp_path: Path) -> None:
    state = tmp_path / "state"
    sandbox = Sandbox(language="py")
    with pytest.raises(SandboxError, match="is not a Python workflow"):
        await sandbox.run(
            "return 1",
            dispatcher=FakeDispatcher(),
            filename="w.js",
            options=RunOptions(state_dir=state),
        )
    assert not (state / "runs").exists() or not list((state / "runs").iterdir())


async def test_extract_meta_py_in_process() -> None:
    meta = await extract_meta("meta = {'name': 'w'}\n", node=None, filename="w.py")
    assert meta == {"name": "w"}
    with pytest.raises(MetaError):
        await extract_meta("meta = {'a': f()}\n", node=None, filename="w.py")


async def test_run_script_js_needs_node() -> None:
    with pytest.raises(NodeNotFoundError):
        await run_script("return 1", node=None, dispatcher=FakeDispatcher(), filename="w.js")
