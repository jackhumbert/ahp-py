"""Error kinds reach the journal from the in-process and remote dispatchers (M16)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_protocol.transport import Transport, memory_pair

from ahp_host_flow.config import ProviderConfig
from ahp_host_flow.engine import load_journal
from tests.flow_hosting import boot_flow
from tests.hosting import run_turn
from tests.limits_fakes import ScriptedProvider

TOML = '[providers.scripted]\nload = "tests.limits_fakes:ScriptedProvider"\n'
REMOTE_TOML = '[providers.far]\nurl = "wss://far.example.com"\nagent = "scripted"\n'


def workflow(prompt: str) -> str:
    return (
        "meta = {'name': 'fail', 'description': 'd'}\n"
        "\n"
        "async def main(args):\n"
        f"    return await agent({prompt!r}, label='Fail')\n"
    )


def kinds(base: Path) -> list[str | None]:
    (run,) = (base / "state" / "runs").iterdir()
    return [a.kind for a in load_journal(base / "state", run.name).agents]


async def _failing_run(tmp_path: Path, toml: str, prompt: str, **boot: object) -> list[str | None]:
    user = tmp_path / "user"
    user.mkdir()
    (user / "fail.py").write_text(workflow(prompt), encoding="utf-8")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), toml, **boot)  # type: ignore[arg-type]
    try:
        await run_turn(fx.wire, fx.chat, "t1", "/fail")
    finally:
        await fx.shut()
    return kinds(tmp_path)


async def test_errkind_dispatch_refusal(tmp_path: Path) -> None:
    assert await _failing_run(tmp_path, TOML, "fail:acp.refusal") == ["refused"]


async def test_errkind_dispatch_agent_exited(tmp_path: Path) -> None:
    assert await _failing_run(tmp_path, TOML, "fail:acp.agentExited") == ["unavailable"]


async def test_errkind_dispatch_claude_rate_limited(tmp_path: Path) -> None:
    prompt = "fail:claude.error_during_execution:resumable"
    assert await _failing_run(tmp_path, TOML, prompt) == ["rate_limited"]


async def test_errkind_dispatch_provider_load(tmp_path: Path) -> None:
    toml = '[providers.broken]\nload = "tests.flow_fakes:not_a_provider"\n'
    assert await _failing_run(tmp_path, toml, "hello") == ["unavailable"]


async def test_errkind_dispatch_remote_turn_failed(tmp_path: Path) -> None:
    far = Host(ScriptedProvider(provider_id="scripted"), LoopbackSingleUserPolicy())
    serving: set[asyncio.Task[None]] = set()

    def dialer(spec: ProviderConfig) -> Callable[[], Awaitable[Transport]]:
        async def dial() -> Transport:
            client_end, server_end = memory_pair()
            task = asyncio.create_task(far.serve(server_end))
            serving.add(task)
            task.add_done_callback(serving.discard)
            return client_end

        return dial

    try:
        found = await _failing_run(
            tmp_path, REMOTE_TOML, "fail:acp.refusal", provider_options={"remote_dialer": dialer}
        )
    finally:
        for task in list(serving):
            task.cancel()
    assert found == ["refused"]
