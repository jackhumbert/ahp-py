"""The runners start with an allowlisted environment, never the host's whole one."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ahp_host_flow.sandbox import Sandbox, run_script
from ahp_host_flow.sandbox.client import runner_env

from ..fakes import FakeDispatcher  # noqa: TID252

SECRET = "AHP_FLOW_TEST_SECRET"
PLAIN = "AHP_FLOW_TEST_PLAIN"


def test_sandbox_env_keeps_only_the_allowlist() -> None:
    base = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "HOME": "/Users/me",
        "NODE_OPTIONS": "--require /tmp/x.js",
        "OPENAI_API_KEY": "k",
        SECRET: "s",
    }
    assert runner_env(base) == {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }


async def test_sandbox_env_py_script_sees_no_host_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SECRET, "s3cr3t")
    monkeypatch.setenv(PLAIN, "plain")
    source = (
        "import os\n"
        "async def main(args):\n"
        f"    return [os.environ.get({SECRET!r}), os.environ.get({PLAIN!r}),\n"
        "            's3cr3t' in repr(dict(os.environ))]\n"
    )
    result = await run_script(source, node=None, dispatcher=FakeDispatcher(), filename="w.py")
    assert result == [None, None, False]


@pytest.mark.node
async def test_sandbox_env_node_process_env_has_no_host_secret(
    node_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stands in for a script that reached `process` by any means: the runner's own
    # process.env is what it would find.
    monkeypatch.setenv(SECRET, "s3cr3t")
    monkeypatch.setenv("TZ", "UTC")
    dump = tmp_path / "dump.mjs"
    dump.write_text("process.stderr.write(JSON.stringify(process.env) + '\\n')\n")
    sandbox = Sandbox(node_path, runner=dump)
    await sandbox.start()
    assert sandbox._proc is not None
    await asyncio.wait_for(sandbox._proc.wait(), 30)
    await sandbox.aclose()
    tail = sandbox.stderr_tail
    assert '"PATH"' in tail
    assert '"TZ":"UTC"' in tail
    assert SECRET not in tail
    assert "s3cr3t" not in tail
