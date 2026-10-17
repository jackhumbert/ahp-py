"""Per-run wall-clock timeout and stdout-reader hardening."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ahp_host_flow.engine import RunOptions
from ahp_host_flow.sandbox import RunTimeoutError, Sandbox, SandboxError, run_script

from ..fakes import FakeDispatcher  # noqa: TID252

pytestmark = pytest.mark.node

SHORT = RunOptions(timeout=0.5)


async def _run(source: str, node: str, dispatcher: FakeDispatcher, **kwargs: Any) -> Any:
    return await asyncio.wait_for(
        run_script(source, node=node, dispatcher=dispatcher, **kwargs), 30
    )


async def test_run_timeout_busy_loop(node_path: str) -> None:
    with pytest.raises(RunTimeoutError) as info:
        await _run("while (true) {}", node_path, FakeDispatcher(), options=SHORT)
    assert str(info.value) == "run timed out after 0.5 s"


async def test_run_timeout_after_await(node_path: str) -> None:
    source = "await agent('a'); while (true) {}"
    with pytest.raises(RunTimeoutError):
        await _run(source, node_path, FakeDispatcher(), options=SHORT)


async def test_run_timeout_hung_agent(node_path: str) -> None:
    d = FakeDispatcher()
    with pytest.raises(RunTimeoutError):
        await _run("return await agent('hang')", node_path, d, options=SHORT)
    assert d.cancelled == [0]


async def test_run_timeout_sandbox_dead_after(node_path: str) -> None:
    async def scenario() -> None:
        async with Sandbox(node_path) as sandbox:
            with pytest.raises(RunTimeoutError):
                await sandbox.run("while (true) {}", dispatcher=FakeDispatcher(), options=SHORT)
            with pytest.raises(SandboxError):
                await sandbox.run("return 1", dispatcher=FakeDispatcher())

    await asyncio.wait_for(scenario(), 30)


async def test_run_timeout_not_hit(node_path: str) -> None:
    result = await _run("return 1", node_path, FakeDispatcher(), options=RunOptions(timeout=30))
    assert result == 1


async def test_run_timeout_reader_failure(node_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Sandbox, msg: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(Sandbox, "_dispatch", boom)
    with pytest.raises(SandboxError, match="sandbox reader failed"):
        await _run('return await agent("x")', node_path, FakeDispatcher())
