"""A named chat (`opts.x.chat`) survives a restart where its provider resumes sessions (M17.3a).

The first call replays from the journal, so only the cut-off second call is live after the
restart: it finds the chat's session rebuilt from the resume state, or recreated.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.flow_fakes import RestartHoldProvider, ResumableHoldProvider, reset_fakes
from tests.flow_hosting import boot_flow, final_json, restart_flow, store_for
from tests.hosting import notifications, start_turn
from tests.integration.test_flow_durability import resume_by_hand, wait_resumed

NO_NODE = "no-node"
WORKFLOW = (
    "meta = {'name': 'durnamed', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await agent('one', x={'chat': 'p'})\n"
    "    b = await agent('hold two', x={'chat': 'p'})\n"
    "    return {'a': a, 'b': b}\n"
)
TOML_RESUMABLE = (
    '[providers.rh]\nload = "tests.flow_fakes:ResumableHoldProvider"\n\n[routing]\ndefault = "rh"\n'
)
TOML_PLAIN = (
    '[providers.hold]\nload = "tests.flow_fakes:RestartHoldProvider"\n\n'
    '[routing]\ndefault = "hold"\n'
)


async def _cut_off(
    tmp_path: Path, node: str, toml: str, prompts: list[str], *, drain: bool
) -> None:
    """Run the workflow until `hold two` is in flight, then stop the host."""
    reset_fakes()
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "durnamed.py").write_text(WORKFLOW, encoding="utf-8")
    fx = await boot_flow(tmp_path, node, toml, store=store_for(tmp_path))
    await start_turn(fx.wire, fx.chat, "t1", "/durnamed")
    assert await fx.wire.until(lambda: "hold two" in prompts, timeout=10)
    if drain:
        assert fx.chat in await fx.host.drain(0.2)
    await fx.shut()


async def _resumable(tmp_path: Path, *, drain: bool) -> None:
    node = str(tmp_path / NO_NODE)
    await _cut_off(tmp_path, node, TOML_RESUMABLE, ResumableHoldProvider.prompts, drain=drain)
    ResumableHoldProvider.holding = False
    fx2 = await restart_flow(tmp_path, node, TOML_RESUMABLE)
    try:
        if not drain:
            await asyncio.sleep(0.3)
            await resume_by_hand(fx2)
        turn = await wait_resumed(fx2)
        assert final_json(turn) == {"a": "mem: one", "b": "mem: one | hold two"}
        assert ResumableHoldProvider.sessions_resumed == 1
        assert ResumableHoldProvider.sessions_created == 1
        assert (
            "agent() #1: opts.x.chat 'p' resumed its session on provider 'rh' after the restart"
            in notifications(turn)
        )
    finally:
        await fx2.shut()


async def test_durnamed_resumable_remembers_after_drain(tmp_path: Path) -> None:
    await _resumable(tmp_path, drain=True)


async def test_durnamed_resumable_remembers_after_crash(tmp_path: Path) -> None:
    await _resumable(tmp_path, drain=False)


async def test_durnamed_non_resumable_recreated_with_line(tmp_path: Path) -> None:
    node = str(tmp_path / NO_NODE)
    await _cut_off(tmp_path, node, TOML_PLAIN, RestartHoldProvider.prompts, drain=True)
    RestartHoldProvider.holding = False
    fx2 = await restart_flow(tmp_path, node, TOML_PLAIN)
    try:
        turn = await wait_resumed(fx2)
        assert final_json(turn) == {"a": "rec: one", "b": "rec: hold two"}
        assert RestartHoldProvider.sessions_created == 2
        assert (
            "agent() #1: opts.x.chat 'p' was recreated after the restart; provider 'hold' "
            "cannot resume its session, so it starts without the earlier turns"
            in notifications(turn)
        )
    finally:
        await fx2.shut()


async def test_durnamed_no_recorded_state_says_so(tmp_path: Path) -> None:
    """A resumable provider that recorded no state: recreated, and the line names that cause."""
    node = str(tmp_path / NO_NODE)
    reset_fakes()
    ResumableHoldProvider.records_state = False
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "durnamed.py").write_text(WORKFLOW, encoding="utf-8")
    fx = await boot_flow(tmp_path, node, TOML_RESUMABLE, store=store_for(tmp_path))
    await start_turn(fx.wire, fx.chat, "t1", "/durnamed")
    assert await fx.wire.until(lambda: "hold two" in ResumableHoldProvider.prompts, timeout=10)
    assert fx.chat in await fx.host.drain(0.2)
    await fx.shut()
    ResumableHoldProvider.holding = False
    fx2 = await restart_flow(tmp_path, node, TOML_RESUMABLE)
    try:
        turn = await wait_resumed(fx2)
        assert final_json(turn) == {"a": "mem: one", "b": "mem: hold two"}
        assert ResumableHoldProvider.sessions_resumed == 0
        assert (
            "agent() #1: opts.x.chat 'p' was recreated after the restart; provider 'rh' "
            "recorded no session state for it, so it starts without the earlier turns"
            in notifications(turn)
        )
    finally:
        await fx2.shut()
