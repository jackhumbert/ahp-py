"""Custom verbs on the wire: the `verb` request, the host re-check, children, abort, masking."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import AbortSignal, Redactor, RunOptions, VerbCall
from ahp_host_flow.sandbox import protocol, run_script

from ..fakes import FakeDispatcher  # noqa: TID252
from ..hook_fakes import FakeVerbHost, FakeVerbs  # noqa: TID252

RUNNER = Path(protocol.__file__).with_name("runner.mjs")

# Every route below must yield 'undefined' (no host global) or throw in the script realm.
REACH = "return [typeof process, typeof require, typeof workflowParent].join()"
NO_HOST = "undefined,undefined,undefined"

UNKNOWN_EVIL = "unknown verb 'evil': this run declares tool"


def test_customverb_sandbox_protocol_code_matches() -> None:
    match = re.search(r"const VERB_ERROR = (-\d+);", RUNNER.read_text(encoding="utf-8"))
    assert match is not None
    assert int(match.group(1)) == protocol.VERB_ERROR


async def _run(
    source: str,
    filename: str,
    node: str | None,
    fake: FakeVerbs,
    *,
    dispatcher: FakeDispatcher | None = None,
    options: RunOptions | None = None,
    **kwargs: Any,
) -> Any:
    if options is None:
        options = RunOptions(timeout=60, verbs=fake.handlers)
    return await run_script(
        source,
        node=node,
        dispatcher=dispatcher or FakeDispatcher(),
        filename=filename,
        options=options,
        **kwargs,
    )


@pytest.mark.node
async def test_customverb_sandbox_hostile_runner_js(node_path: str) -> None:
    fake = FakeVerbs({"tool": "echo"})
    source = (
        "const real = JSON.stringify\n"
        "JSON.stringify = (v) => real({...v, name: 'evil'})\n"
        "let message = null\n"
        "try { await tool('x') } catch (e) { message = e.message }\n"
        "JSON.stringify = real\n"
        "return ['refused', message]\n"
    )
    assert await _run(source, "w.js", node_path, fake) == ["refused", UNKNOWN_EVIL]
    assert fake.calls == []


async def test_customverb_sandbox_hostile_runner_py() -> None:
    fake = FakeVerbs({"tool": "echo"})
    source = (
        "from ahp_host_flow.sandbox import pyrunner\n"
        "async def main(args):\n"
        "    msg = await pyrunner.current_runtime().link.call("
        "'verb', {'name': 'evil', 'args': []})\n"
        "    return msg['error']['message']\n"
    )
    assert await _run(source, "w.py", None, fake) == UNKNOWN_EVIL
    assert fake.calls == []


@pytest.mark.node
@pytest.mark.parametrize(
    "route",
    [
        "tool.constructor",
        "tool('x').constructor.constructor",
        "(await tool('x')).constructor.constructor",
        "(await (async () => { try { await boom(1) } catch (e) { return e } })())"
        ".constructor.constructor",
    ],
)
async def test_customverb_sandbox_values_stay_in_realm(node_path: str, route: str) -> None:
    fake = FakeVerbs({"tool": "echo", "boom": "fail"})
    source = f"const F = {route};\nreturn F('{REACH}')()\n"
    assert await _run(source, "w.js", node_path, fake) == NO_HOST


@pytest.mark.node
async def test_customverb_sandbox_children_js(node_path: str, tmp_path: Path) -> None:
    (tmp_path / "child.js").write_text("return await tool('cj')\n", encoding="utf-8")
    (tmp_path / "child.py").write_text(
        "async def main(args):\n    return await tool('cp')\n", encoding="utf-8"
    )
    fake = FakeVerbs({"tool": "echo"})
    source = (
        "const a = await tool('p')\n"
        "const b = await workflow({scriptPath: 'child.js'})\n"
        "const c = await workflow({scriptPath: 'child.py'})\n"
        "return [a, b, c]\n"
    )
    result = await _run(
        source,
        "parent.js",
        node_path,
        fake,
        options=RunOptions(run_root=tmp_path, timeout=60, verbs=fake.handlers),
    )
    assert result == [{"name": "tool", "args": [x]} for x in ("p", "cj", "cp")]
    assert sorted([c.ordinal, c.name, list(c.args)] for c in fake.calls) == [
        [0, "tool", ["p"]],
        [1, "tool", ["cj"]],
        [2, "tool", ["cp"]],
    ]


async def test_customverb_sandbox_child_py(tmp_path: Path) -> None:
    (tmp_path / "child.py").write_text(
        "async def main(args):\n    return await tool('c')\n", encoding="utf-8"
    )
    fake = FakeVerbs({"tool": "echo"})
    source = (
        "async def main(args):\n"
        "    a = await tool('p')\n"
        "    b = await workflow({'scriptPath': 'child.py'})\n"
        "    return [a, b]\n"
    )
    result = await _run(
        source,
        "parent.py",
        None,
        fake,
        options=RunOptions(run_root=tmp_path, timeout=60, verbs=fake.handlers),
    )
    assert result == [{"name": "tool", "args": ["p"]}, {"name": "tool", "args": ["c"]}]
    assert sorted(c.ordinal for c in fake.calls) == [0, 1]


async def test_customverb_sandbox_not_agents() -> None:
    fake = FakeVerbs({"tool": "echo"})
    source = (
        "async def main(args):\n"
        "    got = [await tool('x') for _ in range(3)]\n"
        "    try:\n"
        "        await agent('a')\n"
        "    except Exception as exc:\n"
        "        return [got, str(exc)]\n"
    )
    options = RunOptions(
        max_agents=1, budget_total=0, concurrency=1, timeout=60, verbs=fake.handlers
    )
    result = await _run(source, "w.py", None, fake, options=options)
    assert result == [
        [{"name": "tool", "args": ["x"]}] * 3,
        "budget exhausted: 0 of 0 output tokens spent",
    ]

    # An un-awaited agent holds the only slot; an awaited verb call is not held up by it.
    dispatcher = FakeDispatcher()

    async def after_agent_started(call: VerbCall) -> str:
        await dispatcher.wait_started("hang a")
        return "done"

    options = RunOptions(concurrency=1, timeout=60, verbs={"tool": after_agent_started})
    source = "async def main(args):\n    agent('hang a')\n    return await tool('x')\n"
    result = await asyncio.wait_for(
        _run(source, "w.py", None, fake, dispatcher=dispatcher, options=options), 30
    )
    assert result == "done"


async def test_customverb_sandbox_masking() -> None:
    fake = FakeVerbs({"tool": "echo"})
    host = FakeVerbHost()
    secret = "token=abcdefgh12345678"
    source = f"async def main(args):\n    return await tool({secret!r})\n"
    options = RunOptions(timeout=60, verbs=fake.handlers, redact=Redactor().mask_value)
    result = await _run(source, "w.py", None, fake, options=options, verb_host=host)
    assert result == {"name": "tool", "args": ["token=[redacted]"]}
    # The handler gets what the script sent; what leaves the run is masked.
    assert fake.calls[0].args == (secret,)
    assert host.started_calls[0][1] == ["token=[redacted]"]
    finished = host.finished_calls[0]
    assert finished[1:] == (True, {"name": "tool", "args": ["token=[redacted]"]}, None)


async def test_customverb_sandbox_py_replaced_builtin() -> None:
    fake = FakeVerbs({"approve": "echo"})
    source = (
        "from ahp_host_flow.verbs import approve as a2\n"
        "async def main(args):\n"
        "    got = await approve('ada', 's')\n"
        "    try:\n"
        "        await a2('s')\n"
        "    except TypeError as exc:\n"
        "        return [got, str(exc)]\n"
    )
    result = await _run(source, "w.py", None, fake)
    assert result == [
        {"name": "approve", "args": ["ada", "s"]},
        "approve() is a custom verb in this run: call it as a global or with "
        "ahp_host_flow.verbs.verb('approve', ...)",
    ]


async def test_customverb_sandbox_abort_py() -> None:
    fake = FakeVerbs({"hang": "hang"})
    abort = AbortSignal()
    source = (
        "async def main(args):\n"
        "    try:\n"
        "        await hang(1)\n"
        "    except Exception as exc:\n"
        "        return str(exc)\n"
    )
    task = asyncio.ensure_future(_run(source, "w.py", None, fake, abort=abort))
    async with asyncio.timeout(30):
        while not fake.calls:
            await asyncio.sleep(0.01)
    abort.abort()
    assert await asyncio.wait_for(task, 30) == "hang() aborted"
