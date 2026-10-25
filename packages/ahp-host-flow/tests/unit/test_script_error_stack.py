"""A script error's stack names only the script's own frames, never a host path."""

from __future__ import annotations

from pathlib import Path

import pytest

import ahp_host_flow
from ahp_host_flow.sandbox import ScriptError, run_script

from ..fakes import FakeDispatcher  # noqa: TID252

# Where the package (runner.mjs, pyrunner.py, verbs.py) is installed on this host.
HOST_DIR = str(Path(ahp_host_flow.__file__).parent)


def _host_free(stack: str) -> None:
    assert HOST_DIR not in stack
    for marker in ("runner.mjs", "pyrunner.py", "node:internal", "file://", "verbs.py"):
        assert marker not in stack, stack


@pytest.mark.node
@pytest.mark.parametrize(
    "body",
    [
        "throw new Error('boom')",
        # Thrown from inside a verb's own code, and from a JS builtin the script called.
        "await agent(5)",
        "JSON.parse('{')",
        "function f() { throw new TypeError('deep') }\nf()",
    ],
)
async def test_script_error_stack_js_keeps_only_script_frames(node_path: str, body: str) -> None:
    with pytest.raises(ScriptError) as info:
        await run_script(body + "\n", node=node_path, dispatcher=FakeDispatcher(), filename="w.js")
    stack = info.value.stack
    assert stack is not None
    _host_free(stack)
    if "boom" in body or "deep" in body:
        assert "w.js:" in stack


@pytest.mark.parametrize(
    "body",
    [
        "    raise ValueError('boom')",
        "    await agent(5)",
        "    def f():\n        raise TypeError('deep')\n    f()",
    ],
)
async def test_script_error_stack_py_keeps_only_script_frames(body: str) -> None:
    source = f"async def main(args):\n{body}\n"
    with pytest.raises(ScriptError) as info:
        await run_script(source, node=None, dispatcher=FakeDispatcher(), filename="w.py")
    stack = info.value.stack
    assert stack is not None
    _host_free(stack)
    if "boom" in body or "deep" in body:
        assert 'File "w.py"' in stack
