"""The Python runner: meta reading, the wire protocol, and the stdlib-only rule."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow import verbs
from ahp_host_flow.engine import MAX_AGENTS_PER_RUN, MAX_ITEMS_PER_CALL
from ahp_host_flow.sandbox import protocol
from ahp_host_flow.sandbox.pyrunner import RpcError, read_meta
from tests.pyrunner_wire import PyRunnerWire


def test_pyrunner_meta_literal() -> None:
    src = "meta = {'name': 'x', 'tags': ('a', 'b'), 'n': {'deep': [1, 2.5, None, True]}}\n"
    assert read_meta(src) == {
        "name": "x",
        "tags": ["a", "b"],
        "n": {"deep": [1, 2.5, None, True]},
    }


def test_pyrunner_meta_absent() -> None:
    assert read_meta("async def main(args):\n    return 1\n") is None


@pytest.mark.parametrize(
    ("source", "code", "message"),
    [
        (
            "meta = dict(a=1)\n",
            protocol.META_ERROR,
            "meta must be a dict literal (line 1)",
        ),
        (
            "meta = [1]\n",
            protocol.META_ERROR,
            "meta must be a dict literal (line 1)",
        ),
        (
            "meta = {}\nmeta = {}\n",
            protocol.META_ERROR,
            "meta must be assigned once, as a literal (line 2)",
        ),
        (
            "import os as meta\n",
            protocol.META_ERROR,
            "meta must be assigned once, as a literal (line 1)",
        ),
        (
            "meta = {'s': {1, 2}}\n",
            protocol.META_ERROR,
            "meta is not JSON-like: meta.s is a set (line 1)",
        ),
        (
            "meta = {1: 'a'}\n",
            protocol.META_ERROR,
            "meta is not JSON-like: meta has a non-string key (line 1)",
        ),
        (
            "meta = {'a': x}\n",
            protocol.META_ERROR,
            "meta is not a pure literal (line 1)",
        ),
        ("def (:\n", protocol.SYNTAX_ERROR, None),
    ],
)
def test_pyrunner_meta_errors(source: str, code: int, message: str | None) -> None:
    with pytest.raises(RpcError) as info:
        read_meta(source, "w.py")
    assert info.value.code == code
    if message is not None:
        assert info.value.message == message
    else:
        assert info.value.data == {"filename": "w.py"}


def test_pyrunner_meta_never_runs(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    source = f"open({str(marker)!r}, 'w').close()\nmeta = {{'a': 1}}\n"
    assert read_meta(source) == {"a": 1}
    assert not marker.exists()


@pytest.fixture
async def wire() -> AsyncIterator[PyRunnerWire]:
    w = PyRunnerWire()
    await w.start()
    try:
        yield w
    finally:
        if w.proc is not None:
            if w.proc.returncode is None:
                w.proc.kill()
            await w.proc.wait()
            if w.proc.stdin is not None:
                w.proc.stdin.close()


async def _run(w: PyRunnerWire, source: str, rid: int = 1, **params: Any) -> None:
    await w.send(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "run",
            "params": {"source": source, "filename": "w.py", **params},
        }
    )


async def test_pyrunner_wire_meta_and_run(wire: PyRunnerWire) -> None:
    await wire.send(
        {"jsonrpc": "2.0", "id": 1, "method": "meta", "params": {"source": "meta = {'a': (1,)}"}}
    )
    assert (await wire.recv())["result"] == {"meta": {"a": [1]}}
    source = (
        "async def main(args):\n"
        "    log('hi')\n"
        "    phase('P')\n"
        "    v = await agent('a', label='L')\n"
        "    return [v, args]\n"
    )
    await _run(wire, source, 2, args={"k": "é"})
    assert await wire.recv() == {"jsonrpc": "2.0", "method": "log", "params": {"message": "hi"}}
    assert await wire.recv() == {"jsonrpc": "2.0", "method": "phase", "params": {"title": "P"}}
    req = await wire.recv()
    assert req["method"] == "agent"
    assert req["params"] == {"prompt": "a", "opts": {"label": "L"}, "phase": None, "label": "L"}
    await wire.send({"jsonrpc": "2.0", "id": req["id"], "result": {"value": "A", "spent": 3}})
    done = await wire.recv()
    assert done["id"] == 2
    assert done["result"] == {"value": ["A", {"k": "é"}]}
    for raw in wire.raw_lines:
        raw.decode("ascii")
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\r\n")


async def test_pyrunner_wire_run_active(wire: PyRunnerWire) -> None:
    await _run(wire, "async def main(args):\n    await agent('a')\n", 1)
    req = await wire.recv()
    assert req["method"] == "agent"
    await _run(wire, "async def main(args):\n    return 1\n", 2)
    busy = await wire.recv()
    assert busy["id"] == 2
    assert busy["error"]["code"] == protocol.RUN_ACTIVE
    await wire.send({"jsonrpc": "2.0", "id": req["id"], "result": {"value": None}})
    assert (await wire.recv())["id"] == 1


async def test_pyrunner_wire_print_goes_to_stderr(wire: PyRunnerWire) -> None:
    await _run(wire, "async def main(args):\n    print('noise')\n    return 1\n")
    assert (await wire.recv())["result"] == {"value": 1}
    assert await wire.close() == 0
    assert "noise" in await wire.stderr_text()


async def test_pyrunner_wire_bad_json(wire: PyRunnerWire) -> None:
    assert wire.proc is not None
    assert wire.proc.stdin is not None
    wire.proc.stdin.write(b"{nope\n")
    msg = await wire.recv()
    assert msg["id"] is None
    assert msg["error"]["code"] == protocol.PARSE_ERROR


async def test_pyrunner_wire_limits(wire: PyRunnerWire) -> None:
    source = (
        "async def main(args):\n"
        "    out = []\n"
        "    async def attempt(coro_fn):\n"
        "        try:\n"
        "            await coro_fn()\n"
        "        except Exception as exc:\n"
        "            out.append(str(exc))\n"
        "    await attempt(lambda: agent('x'))\n"
        "    await attempt(lambda: parallel([lambda: 1, lambda: 2, lambda: 3]))\n"
        "    await attempt(lambda: pipeline([1, 2, 3], lambda p, i, n: p))\n"
        "    return out\n"
    )
    await _run(
        wire,
        source,
        limits={"maxAgents": 1000, "budgetTotal": 5, "maxItems": 2},
        spent=5,
    )
    done = await wire.recv()
    assert done["result"]["value"] == [
        "budget exhausted: 5 of 5 output tokens spent",
        "parallel() takes at most 2 items, got 3",
        "pipeline() takes at most 2 items, got 3",
    ]


async def test_pyrunner_wire_child_flag(wire: PyRunnerWire) -> None:
    source = (
        "async def main(args):\n"
        "    try:\n"
        "        await workflow('x')\n"
        "    except RuntimeError as exc:\n"
        "        return str(exc)\n"
    )
    await _run(wire, source, child=True)
    assert (await wire.recv())["result"] == {"value": protocol.NEST_MESSAGE}


async def test_pyrunner_wire_workflow_value_reply(wire: PyRunnerWire) -> None:
    source = (
        "async def main(args):\n"
        "    v = await workflow('c', {'a': 1})\n"
        "    return [v, budget.spent()]\n"
    )
    await _run(wire, source)
    req = await wire.recv()
    assert req["method"] == "workflow"
    assert req["params"] == {"ref": "c", "args": {"a": 1}}
    await wire.send({"jsonrpc": "2.0", "id": req["id"], "result": {"value": 7, "spent": 9}})
    assert (await wire.recv())["result"] == {"value": [7, 9]}


async def test_pyrunner_wire_workflow_source_reply(wire: PyRunnerWire) -> None:
    source = "async def main(args):\n    return await workflow('c')\n"
    child = (
        "async def main(args):\n"
        "    v = await agent('inner')\n"
        "    try:\n"
        "        await workflow('d')\n"
        "    except RuntimeError as exc:\n"
        "        return [v, str(exc), args]\n"
    )
    await _run(wire, source)
    req = await wire.recv()
    assert req["method"] == "workflow"
    assert "args" not in req["params"]
    await wire.send(
        {
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": {"source": child, "filename": "c.py", "spent": 0},
        }
    )
    inner = await wire.recv()
    assert inner["method"] == "agent"
    assert inner["params"]["prompt"] == "inner"
    await wire.send({"jsonrpc": "2.0", "id": inner["id"], "result": {"value": "I"}})
    assert (await wire.recv())["result"] == {"value": ["I", protocol.NEST_MESSAGE, None]}


async def test_pyrunner_wire_issue_order(wire: PyRunnerWire) -> None:
    source = (
        "async def main(args):\n"
        "    f = agent('a')\n"
        "    g = agent('b')\n"
        "    return [await f, await g]\n"
    )
    await _run(wire, source)
    first = await wire.recv()
    second = await wire.recv()
    assert [first["params"]["prompt"], second["params"]["prompt"]] == ["a", "b"]
    await wire.send({"jsonrpc": "2.0", "id": second["id"], "result": {"value": "B"}})
    await wire.send({"jsonrpc": "2.0", "id": first["id"], "result": {"value": "A"}})
    assert (await wire.recv())["result"] == {"value": ["A", "B"]}


async def test_pyrunner_wire_cancel_of_the_run_is_not_a_script_error(wire: PyRunnerWire) -> None:
    # EOF cancels the active run; a genuine cancellation must not be turned into a reply.
    await _run(
        wire, "import asyncio\nasync def main(args):\n    log('up')\n    await asyncio.sleep(60)\n"
    )
    assert (await wire.recv())["method"] == "log"
    assert await wire.close() == 0
    assert wire.proc is not None
    assert wire.proc.stdout is not None
    assert await wire.proc.stdout.read() == b""


async def test_pyrunner_wire_eof_exits(wire: PyRunnerWire) -> None:
    assert await wire.close() == 0


def test_pyrunner_stdlib_only() -> None:
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import ahp_host_flow.sandbox.pyrunner, ahp_host_flow.verbs\n"
        "print(chr(10).join(sorted(set(sys.modules) - before)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    ).stdout
    ours = {
        "ahp_host_flow",
        "ahp_host_flow.sandbox",
        "ahp_host_flow.sandbox.protocol",
        "ahp_host_flow.sandbox.pyrunner",
        "ahp_host_flow.verbs",
    }
    for name in out.split():
        assert name in ours or name.split(".")[0] in sys.stdlib_module_names, name


def test_pyrunner_constants_match_engine() -> None:
    assert protocol.DEFAULT_MAX_AGENTS == MAX_AGENTS_PER_RUN
    assert protocol.DEFAULT_MAX_ITEMS == MAX_ITEMS_PER_CALL


def test_verbs_outside_runner() -> None:
    for call in (lambda: verbs.agent("x"), lambda: verbs.budget.spent(), lambda: verbs.args):
        with pytest.raises(verbs.OutsideRunnerError, match="only works inside a workflow run"):
            call()
