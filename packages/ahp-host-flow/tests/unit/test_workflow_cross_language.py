"""workflow() across languages: a JS parent with a .py child and the reverse."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import RunOptions
from ahp_host_flow.sandbox import RunTimeoutError, protocol, run_script

from ..fakes import FakeDispatcher  # noqa: TID252

NEST_MESSAGE = protocol.NEST_MESSAGE

PCHILD = (
    "async def main(args):\n"
    "    phase('Child')\n"
    "    return {'reply': await agent('child ' + args['who']), 'args': args}\n"
)
JCHILD = "phase('J')\nreturn { reply: await agent('child ' + args.who), args }\n"
JS_PARENT = (
    "await agent('a')\n"
    "const r = await workflow('pchild', { who: 'x' })\n"
    "await agent('b')\n"
    "return [r, budget.spent()]\n"
)
PY_PARENT = (
    "async def main(args):\n"
    "    await agent('a')\n"
    "    r = await workflow('jchild', {'who': 'x'})\n"
    "    await agent('b')\n"
    "    return [r, budget.spent()]\n"
)


def _setup(tmp_path: Path, **files: str) -> tuple[Path, dict[str, Any]]:
    wf = tmp_path / "wf"
    wf.mkdir()
    for name, text in files.items():
        (wf / name.replace("__", ".")).write_text(text, encoding="utf-8")
    return wf, {"workflow_dirs": (wf,), "run_root": tmp_path}


async def _run(
    source: str | None,
    node: str | None,
    dispatcher: FakeDispatcher,
    options: RunOptions,
    *,
    filename: str,
    **kwargs: Any,
) -> Any:
    return await asyncio.wait_for(
        run_script(
            source,
            node=node,
            dispatcher=dispatcher,
            options=options,
            filename=filename,
            **kwargs,
        ),
        60,
    )


def _calls(dispatcher: FakeDispatcher) -> list[tuple[int, str, str | None]]:
    return [(c.ordinal, c.prompt, c.phase) for c in dispatcher.calls]


@pytest.mark.node
async def test_workflow_cross_js_parent_py_child(node_path: str, tmp_path: Path) -> None:
    _, paths = _setup(tmp_path, pchild__py=PCHILD)
    dispatcher = FakeDispatcher(output_tokens=10)
    options = RunOptions(budget_total=100, **paths)
    result = await _run(JS_PARENT, node_path, dispatcher, options, filename="p.js")
    r, spent = result
    assert r == {"reply": "child x", "args": {"who": "x"}}
    assert _calls(dispatcher) == [(0, "a", None), (1, "child x", "Child"), (2, "b", "Child")]
    assert spent == 30


@pytest.mark.node
async def test_workflow_cross_py_parent_js_child(node_path: str, tmp_path: Path) -> None:
    _, paths = _setup(tmp_path, jchild__js=JCHILD)
    dispatcher = FakeDispatcher(output_tokens=10)
    options = RunOptions(budget_total=100, **paths)
    result = await _run(PY_PARENT, node_path, dispatcher, options, filename="p.py")
    r, spent = result
    assert r == {"reply": "child x", "args": {"who": "x"}}
    assert _calls(dispatcher) == [(0, "a", None), (1, "child x", "J"), (2, "b", "J")]
    assert spent == 30


@pytest.mark.node
async def test_workflow_cross_child_cannot_nest(node_path: str, tmp_path: Path) -> None:
    _, paths = _setup(
        tmp_path,
        pnest__py="async def main(args):\n    return await workflow('pchild')\n",
        pchild__py=PCHILD,
        jnest__js="return await workflow('jchild')\n",
        jchild__js=JCHILD,
    )
    options = RunOptions(**paths)
    js_parent = "try { await workflow('pnest') } catch (e) { return e.message }"
    py_parent = (
        "async def main(args):\n"
        "    try:\n"
        "        await workflow('jnest')\n"
        "    except Exception as e:\n"
        "        return str(e)\n"
    )
    assert await _run(js_parent, node_path, FakeDispatcher(), options, filename="p.js") == (
        NEST_MESSAGE
    )
    assert await _run(py_parent, node_path, FakeDispatcher(), options, filename="p.py") == (
        NEST_MESSAGE
    )


@pytest.mark.node
async def test_workflow_cross_child_errors(node_path: str, tmp_path: Path) -> None:
    _, paths = _setup(
        tmp_path,
        pbad__py="async def main(args):\n    raise ValueError('bad')\n",
        pbroken__py="async def main(args:\n",
    )
    options = RunOptions(**paths)

    async def message(name: str) -> str:
        source = f"try {{ await workflow('{name}') }} catch (e) {{ return e.message }}"
        result = await _run(source, node_path, FakeDispatcher(), options, filename="p.js")
        assert isinstance(result, str)
        return result

    assert await message("pbad") == "bad"
    broken = await message("pbroken")
    assert broken.startswith("workflow ")
    assert "pbroken.py" in broken


@pytest.mark.node
async def test_workflow_cross_shares_cap(node_path: str, tmp_path: Path) -> None:
    _, paths = _setup(tmp_path, pchild__py=PCHILD)
    options = RunOptions(max_agents=2, **paths)
    source = (
        "await agent('a')\n"
        "await workflow('pchild', { who: 'x' })\n"
        "try { await agent('b') } catch (e) { return e.message }\n"
    )
    dispatcher = FakeDispatcher()
    result = await _run(source, node_path, dispatcher, options, filename="p.js")
    assert result == "agent() limit reached: at most 2 agent() calls per run"
    assert [c.prompt for c in dispatcher.calls] == ["a", "child x"]


@pytest.mark.node
async def test_workflow_cross_journal_resume(node_path: str, tmp_path: Path) -> None:
    wf, paths = _setup(tmp_path, pchild__py=PCHILD)
    state = tmp_path / "state"
    first = FakeDispatcher(output_tokens=10)
    options = RunOptions(state_dir=state, run_id="r1", **paths)
    value = await _run(JS_PARENT, node_path, first, options, filename="p.js")
    assert len(first.calls) == 3
    (wf / "pchild.py").unlink()
    second = FakeDispatcher(output_tokens=10)
    options = RunOptions(state_dir=state, run_id="r2", resume_from="r1", **paths)
    again = await _run(None, node_path, second, options, filename="p.js")
    assert again == value
    assert second.calls == []


@pytest.mark.node
async def test_workflow_cross_timeout_kills_child(node_path: str, tmp_path: Path) -> None:
    _, paths = _setup(tmp_path, pspin__py="async def main(args):\n    while True:\n        pass\n")
    options = RunOptions(timeout=3.0, **paths)
    with pytest.raises(RunTimeoutError):
        await _run(
            "return await workflow('pspin')", node_path, FakeDispatcher(), options, filename="p.js"
        )


@pytest.mark.node
async def test_workflow_cross_python_dirs_refused(node_path: str, tmp_path: Path) -> None:
    _, paths = _setup(tmp_path, pchild__py=PCHILD)
    options = RunOptions(python_dirs=(), **paths)
    source = "try { await workflow('pchild') } catch (e) { return e.message }"
    result = await _run(source, node_path, FakeDispatcher(), options, filename="p.js")
    assert "unknown workflow: 'pchild'" in result


async def test_workflow_cross_js_child_without_node(tmp_path: Path) -> None:
    _, paths = _setup(tmp_path, jchild__js=JCHILD)
    source = (
        "async def main(args):\n"
        "    try:\n"
        "        await workflow('jchild')\n"
        "    except Exception as e:\n"
        "        return str(e)\n"
    )
    result = await _run(source, None, FakeDispatcher(), RunOptions(**paths), filename="p.py")
    assert "no Node >= 20 found to run it" in result
