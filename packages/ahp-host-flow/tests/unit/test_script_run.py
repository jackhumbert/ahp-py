"""Running scripts in a fresh `node:vm` context with agent/log/phase crossing to Python."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ahp_host_flow.engine import AgentCall, AgentResult, EchoDispatcher
from ahp_host_flow.sandbox import (
    MetaError,
    Sandbox,
    ScriptError,
    ScriptSyntaxError,
    run_script,
)

pytestmark = pytest.mark.node


class Recording(EchoDispatcher):
    """Echoes like EchoDispatcher; `calls` records every call."""


class Failing:
    async def run_agent(self, call: AgentCall) -> AgentResult:
        raise RuntimeError("down")


class Gated:
    """Call 'a' waits for call 'b' to run, so serialised calls would deadlock."""

    def __init__(self) -> None:
        self.event = asyncio.Event()

    async def run_agent(self, call: AgentCall) -> AgentResult:
        if call.prompt == "a":
            await self.event.wait()
        else:
            self.event.set()
        return AgentResult(value=call.prompt)


async def _run(source: str, node: str, **kwargs: Any) -> Any:
    kwargs.setdefault("dispatcher", EchoDispatcher())
    return await asyncio.wait_for(run_script(source, node=node, **kwargs), 30)


async def test_script_run_returns_value(node_path: str) -> None:
    assert await _run("return 1 + 1", node_path) == 2


async def test_script_run_echo_agent(node_path: str) -> None:
    assert await _run("return await agent('hi')", node_path) == "hi"


async def test_script_run_args_verbatim(node_path: str) -> None:
    args = {"a": [1, 2], "b": None}
    assert await _run("return args", node_path, args=args) == args
    assert await _run("return typeof args", node_path) == "undefined"


async def test_script_run_undefined_is_null(node_path: str) -> None:
    assert await _run("", node_path) is None


async def test_script_run_ordinals_at_issue_time(node_path: str) -> None:
    dispatcher = Recording()
    source = "const a = agent('x'); const b = agent('y'); return await Promise.all([a, b])"
    assert await _run(source, node_path, dispatcher=dispatcher) == ["x", "y"]
    assert {(c.ordinal, c.prompt) for c in dispatcher.calls} == {(0, "x"), (1, "y")}


async def test_script_run_calls_are_concurrent(node_path: str) -> None:
    source = "return await Promise.all([agent('a'), agent('b')])"
    assert await _run(source, node_path, dispatcher=Gated()) == ["a", "b"]


async def test_script_run_opts_verbatim(node_path: str) -> None:
    dispatcher = Recording()
    opts = {"label": "L", "phase": "P", "model": "haiku", "x": {"provider": "glm"}}
    source = "await agent('p', {label: 'L', phase: 'P', model: 'haiku', x: {provider: 'glm'}})"
    await _run(source, node_path, dispatcher=dispatcher)
    assert [dict(c.opts) for c in dispatcher.calls] == [opts]


async def test_script_run_log_and_phase(node_path: str) -> None:
    logs: list[str] = []
    phases: list[str] = []
    result = await _run(
        "phase('Map'); log('hello'); return 0",
        node_path,
        on_log=logs.append,
        on_phase=phases.append,
    )
    assert result == 0
    assert phases == ["Map"]
    assert logs == ["hello"]


async def test_script_run_script_error(node_path: str) -> None:
    with pytest.raises(ScriptError) as info:
        await _run("throw new Error('boom')", node_path)
    assert str(info.value) == "boom"
    assert info.value.js_name == "Error"


async def test_script_run_dispatcher_failure_is_null(node_path: str) -> None:
    logs: list[str] = []
    result = await _run(
        "return await agent('x')", node_path, dispatcher=Failing(), on_log=logs.append
    )
    assert result is None
    assert len(logs) == 1
    assert "failed" in logs[0]
    assert "down" in logs[0]


async def test_script_run_no_node_apis(node_path: str) -> None:
    source = "return [typeof require, typeof process, typeof module, typeof console]"
    assert await _run(source, node_path) == ["undefined"] * 4


async def test_script_run_dynamic_import_fails(node_path: str) -> None:
    with pytest.raises((ScriptError, ScriptSyntaxError)):
        await _run("await import('node:fs')", node_path)


@pytest.mark.parametrize(
    "source",
    [
        "return agent.constructor('return typeof process')()",
        "return this.constructor.constructor('return typeof process')()",
        "return globalThis.constructor.constructor('return typeof process')()",
        "return Object.getPrototypeOf(globalThis).constructor.constructor("
        "'return typeof process')()",
        "return (async () => {}).constructor('return typeof process')().then(String)",
        "return typeof RealDate + typeof banned + typeof install",
    ],
)
async def test_script_run_no_host_realm_escape(node_path: str, source: str) -> None:
    assert await _run(source, node_path) in ("undefined", "undefinedundefinedundefined")


async def test_script_run_fresh_context_per_run(node_path: str) -> None:
    async with Sandbox(node_path) as sandbox:
        dispatcher = EchoDispatcher()
        first = await asyncio.wait_for(
            sandbox.run("globalThis.leak = 1; return 1", dispatcher=dispatcher), 30
        )
        second = await asyncio.wait_for(
            sandbox.run("return typeof leak", dispatcher=dispatcher), 30
        )
    assert first == 1
    assert second == "undefined"


async def test_script_run_rejects_non_literal_meta(node_path: str) -> None:
    source = "export const meta = {name: String(1), description: 'd'}\nreturn 1"
    with pytest.raises(MetaError):
        await _run(source, node_path)


async def test_script_run_bad_prompt(node_path: str) -> None:
    with pytest.raises(ScriptError) as info:
        await _run("return await agent(42)", node_path)
    assert "prompt string" in str(info.value)


async def test_script_run_line_separators_round_trip(node_path: str) -> None:
    # Node's readline ends a line at raw U+2028/U+2029; they must cross escaped.
    source = "const s = 'a\u2028b\u2029c'\nreturn [s, args, await agent(s + args)]"
    result = await asyncio.wait_for(
        run_script(source, node=node_path, dispatcher=EchoDispatcher(), args="\u2028"), 30
    )
    assert result == ["a\u2028b\u2029c", "\u2028", "a\u2028b\u2029c\u2028"]
