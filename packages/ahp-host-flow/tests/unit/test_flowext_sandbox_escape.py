"""Adversarial probes of the M10 gate verbs: ask(), approve() and canvas() in the sandbox.

Each probe tries to leave the script realm (host Function, `process`, `require`, the
real Date.now/Math.random, the parent's workflow()) through a new global, a value
ask()/approve()/canvas() return or throw, or their prototype chains; or to get options
past the host that a runner would refuse. Plan §8 M0/M1 records the earlier escapes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import GateRunner, RunOptions
from ahp_host_flow.sandbox import ScriptError, run_script

from ..fakes import FakeDispatcher, FakeGates  # noqa: TID252

# Every route below must yield 'undefined' (no host global) or throw in the script realm.
REACH = "return [typeof process, typeof require, typeof workflowParent].join()"


async def _js(
    source: str,
    node: str,
    gates: FakeGates | None = None,
    *,
    tmp_path: Path | None = None,
    filename: str = "w.js",
) -> Any:
    options = (
        RunOptions(timeout=60) if tmp_path is None else RunOptions(run_root=tmp_path, timeout=60)
    )
    return await run_script(
        source,
        node=node,
        dispatcher=FakeDispatcher(),
        gates=gates if gates is not None else FakeGates(),
        filename=filename,
        options=options,
    )


NO_HOST = "undefined,undefined,undefined"


@pytest.mark.node
@pytest.mark.parametrize(
    "route",
    [
        "ask.constructor",
        "approve.constructor",
        "canvas.constructor",
        "ask.constructor.constructor",
        "Object.getPrototypeOf(ask).constructor",
        "ask('q').constructor.constructor",
        "approve('q').constructor.constructor",
        "Object.getPrototypeOf(ask('q')).then.constructor",
        "(await ask('obj', {schema: {type: 'object'}})).constructor.constructor",
        "(await ask('arr', {schema: {type: 'array'}})).constructor.constructor",
        "Object.getPrototypeOf(await ask('arr', {schema: {type: 'array'}})).map.constructor",
        "(await approve('yes')).constructor.constructor",
    ],
)
async def test_flowext_escape_gate_values_stay_in_realm(node_path: str, route: str) -> None:
    gates = FakeGates(
        answers={"obj": {"a": 1}, "arr": ["x"], "q": "a"},
        approvals={"yes": True, "q": True},
        hang=False,
    )
    source = f"const F = {route};\nreturn F('{REACH}')()\n"
    assert await _js(source, node_path, gates) == NO_HOST


@pytest.mark.node
@pytest.mark.parametrize(
    "throwing",
    [
        "await ask(1)",
        "await approve('s', {diff: 5})",
        "canvas('javascript:alert(1)')",
        "await ask('q', {choices: []})",
    ],
)
async def test_flowext_escape_gate_errors_stay_in_realm(node_path: str, throwing: str) -> None:
    source = (
        f"try {{ {throwing} }} catch (e) {{\n"
        f"  return [e instanceof Error, e.constructor.constructor('{REACH}')()]\n"
        "}\n"
        "return 'no throw'\n"
    )
    assert await _js(source, node_path) == [True, NO_HOST]


@pytest.mark.node
async def test_flowext_escape_no_answer_path_error_in_realm(node_path: str) -> None:
    # gates=None: the host answers GATE_ERROR and the verb throws a script-realm Error.
    source = (
        "try { await ask('q') } catch (e) {\n"
        f"  return [e instanceof Error, e.constructor.constructor('{REACH}')(), e.message]\n"
        "}\n"
    )
    result = await run_script(source, node=node_path, dispatcher=FakeDispatcher(), filename="w.js")
    assert result[:2] == [True, NO_HOST]
    assert "no answer path" in result[2]


@pytest.mark.node
@pytest.mark.parametrize(
    "source",
    [
        "return ask.constructor('return Date.now()')()",
        "return ask.constructor('return Math.random()')()",
        "return (await ask('obj', {schema: {type: 'object'}}))"
        ".constructor.constructor('return Date')().now()",
        "return ask('q').constructor.constructor('return Math')().random()",
    ],
)
async def test_flowext_escape_no_real_nondeterminism(node_path: str, source: str) -> None:
    gates = FakeGates(answers={"obj": {"a": 1}, "q": "a"}, hang=False)
    with pytest.raises(ScriptError, match="not deterministic"):
        await _js(source, node_path, gates)


@pytest.mark.node
async def test_flowext_escape_answer_proto_is_data(node_path: str) -> None:
    gates = FakeGates(answers={"p": {"__proto__": {"polluted": True}, "constructor": "x"}})
    source = (
        "const v = await ask('p', {schema: {type: 'object'}})\n"
        "return [Object.getPrototypeOf(v) === Object.prototype, ({}).polluted === undefined,\n"
        "  Object.hasOwn(v, '__proto__'), v.constructor]\n"
    )
    assert await _js(source, node_path, gates) == [True, True, True, "x"]


@pytest.mark.node
async def test_flowext_escape_answer_is_a_copy(node_path: str) -> None:
    # Mutating one answer cannot reach the host's value or a later answer.
    answer = {"a": [1]}
    gates = FakeGates(answers={"o": answer})
    source = (
        "const o = {schema: {type: 'object'}}\n"
        "const v = await ask('o', o); v.a.push(2); v.b = 1\n"
        "const w = await ask('o', o)\n"
        "return [v === w, w]\n"
    )
    assert await _js(source, node_path, gates) == [False, {"a": [1]}]
    assert answer == {"a": [1]}


@pytest.mark.node
@pytest.mark.parametrize(
    "setup",
    [
        # A toJSON hook sends options the runner's own checks never saw.
        "const c = ['a']; c.toJSON = () => [{evil: 1}]; const p = ask('q', {choices: c})",
        "const c = ['a']; c.toJSON = () => 'abc'; const p = ask('q', {choices: c})",
        "const d = [{path: 'a'}]; d.toJSON = () => [{path: 5}]; const p = approve('q', {diff: d})",
        # A replaced JSON.stringify rewrites the whole request.
        "const real = JSON.stringify;"
        " JSON.stringify = (v) => real({...v, options: {choices: ['a'], schema: {}}});"
        " const p = ask('q')",
        "const real = JSON.stringify;"
        " JSON.stringify = (v) => real({...v, options: {extra: 1}}); const p = ask('q')",
        "const real = JSON.stringify;"
        " JSON.stringify = (v) => real({...v, options: {diff: {path: 'a'}}});"
        " const p = approve('q')",
    ],
)
async def test_flowext_escape_host_validates_options(node_path: str, setup: str) -> None:
    gates = FakeGates(answers={"q": "a"}, hang=False)
    source = f"{setup}\ntry {{ return ['answered', await p] }} catch (e) {{ return ['refused'] }}\n"
    assert await _js(source, node_path, gates) == ["refused"]
    assert gates.calls == []


@pytest.mark.node
async def test_flowext_escape_refused_gate_takes_no_ordinal(node_path: str) -> None:
    gates = FakeGates(answers={"ok": "a"}, hang=False)
    source = (
        "const c = ['a']; c.toJSON = () => [1]\n"
        "try { await ask('bad', {choices: c}) } catch {}\n"
        "return await ask('ok')\n"
    )
    assert await _js(source, node_path, gates) == "a"
    assert [(c.ordinal, c.question) for c in gates.calls] == [(0, "ok")]


@pytest.mark.node
async def test_flowext_escape_canvas_rechecked_at_host(node_path: str) -> None:
    gates = FakeGates()
    source = (
        "const real = JSON.stringify\n"
        "JSON.stringify = (v) => real({url: 'javascript:alert(1)', title: {x: 1}})\n"
        "canvas('https://example.com')\n"
        "JSON.stringify = real\n"
        "canvas('https://example.com/ok', {title: 't'})\n"
        "return 1\n"
    )
    assert await _js(source, node_path, gates) == 1
    assert gates.canvases == [("https://example.com/ok", "t")]


@pytest.mark.node
async def test_flowext_escape_child_has_own_verbs(node_path: str, tmp_path: Path) -> None:
    # A JS child runs in its own context: its gate verbs are its own, it sees none of
    # the parent's globals, its workflow() refuses, and only JSON comes back.
    (tmp_path / "child.js").write_text(
        "const a = await ask('child?')\n"
        "let nested\n"
        "try { await ask.constructor.constructor('return workflow')()('x') }\n"
        "catch (e) { nested = e.message }\n"
        "return {a, marker: typeof parentMarker, nested,\n"
        "  reach: ask.constructor.constructor('" + REACH + "')(), f: ask, p: ask('child?')}\n",
        encoding="utf-8",
    )
    gates = FakeGates(answers={"parent?": "p", "child?": "c"}, hang=False)
    source = (
        "globalThis.parentMarker = 1\n"
        "const p = await ask('parent?')\n"
        "const c = await workflow({scriptPath: 'child.js'})\n"
        "return [p, c, typeof ask]\n"
    )
    result = await _js(source, node_path, gates, tmp_path=tmp_path, filename="parent.js")
    p, child, kind = result
    assert (p, kind) == ("p", "function")
    assert child["a"] == "c"
    assert child["marker"] == "undefined"
    assert "one level" in child["nested"]
    assert child["reach"] == NO_HOST
    # A function and a promise do not survive JSON.
    assert "f" not in child
    assert child["p"] == {}
    # One host counter across parent and child, in line order.
    ordinals = {c.question: c.ordinal for c in gates.calls if c.ordinal < 2}
    assert ordinals == {"parent?": 0, "child?": 1}


async def test_flowext_escape_py_answer_is_plain_json() -> None:
    gates = FakeGates(answers={"o": {"__class__": "x", "a": [1, {"b": None}], "n": 1.5}})
    source = (
        "async def main(args):\n"
        "    v = await ask('o', schema={'type': 'object'})\n"
        "    def plain(x):\n"
        "        if isinstance(x, dict):\n"
        "            return type(x) is dict and all(\n"
        "                type(k) is str and plain(w) for k, w in x.items())\n"
        "        if isinstance(x, list):\n"
        "            return type(x) is list and all(plain(w) for w in x)\n"
        "        return x is None or type(x) in (str, int, float, bool)\n"
        "    return [plain(v), v]\n"
    )
    result = await run_script(
        source, node=None, dispatcher=FakeDispatcher(), gates=gates, filename="w.py"
    )
    assert result == [True, {"__class__": "x", "a": [1, {"b": None}], "n": 1.5}]


async def test_flowext_escape_py_host_validates_options() -> None:
    # The Python runner checks too, but the host does not trust it: a runner bypassed
    # through its link sends bad options and is refused.
    gates = FakeGates(hang=False)
    source = (
        "async def main(args):\n"
        "    from ahp_host_flow.verbs import _runtime\n"
        "    rt = _runtime('ask')\n"
        "    msg = await rt.link.call('gate', {'kind': 'ask', 'question': 'q',\n"
        "                                      'options': {'choices': [1, 2]}})\n"
        "    return msg.get('error', {}).get('message')\n"
    )
    result = await run_script(
        source, node=None, dispatcher=FakeDispatcher(), gates=gates, filename="w.py"
    )
    assert "choices" in str(result)
    assert gates.calls == []


@pytest.mark.parametrize(
    ("kind", "options"),
    [
        ("ask", {"choices": "abc"}),
        ("ask", {"choices": []}),
        ("ask", {"choices": ["a", 1]}),
        ("ask", {"schema": "x"}),
        ("ask", {"choices": ["a"], "schema": {}}),
        ("ask", {"diff": "x"}),
        ("approve", {"diff": 5}),
        ("approve", {"diff": [{"path": 1}]}),
        ("approve", {"diff": [{"path": "a", "before": 1}]}),
        ("approve", {"choices": ["a"]}),
    ],
)
def test_flowext_escape_gate_runner_refuses_bad_options(kind: str, options: dict[str, Any]) -> None:
    runner = GateRunner(FakeGates())
    with pytest.raises(ValueError, match=kind):
        runner.issue(kind, "q", options)
    # A refusal takes no ordinal.
    call, _ = runner.issue("ask", "q", {})
    assert call.ordinal == 0
