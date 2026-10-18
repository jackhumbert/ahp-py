"""Custom verbs across a host restart: finished calls replay, a cut-off one runs again (M12)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ahp_host_flow.engine import load_journal
from tests.flow_fakes import reset_fakes
from tests.flow_hosting import boot_flow, final_json, restart_flow, store_for
from tests.hook_fakes import ECHO_CALLS, HoldVerb, reset_hooks
from tests.hosting import found, notifications, start_turn, state
from tests.integration.test_flow_durability import resume_by_hand, wait_resumed

TOML = (
    '[providers.hold]\nload = "tests.flow_fakes:RestartHoldProvider"\n'
    '\n[routing]\ndefault = "hold"\n'
    '\n[verbs]\ntool = "tests.hook_fakes:echo_verb"\nwait = "tests.hook_fakes:hold_verb"\n'
)
RESTART_JS = "const a = await tool('x')\nconst b = await wait('y')\nreturn {a, b}\n"
RESTART_PY = (
    "meta = {'name': 'restart_py', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await tool('x')\n"
    "    b = await wait('y')\n"
    "    return {'a': a, 'b': b}\n"
)
EXPECTED = {"a": {"name": "tool", "args": ["x"]}, "b": "released"}


async def _check(tmp_path: Path, node: str, command: str, *, drain: bool) -> None:
    reset_fakes()
    reset_hooks()
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "restart.js").write_text(RESTART_JS, encoding="utf-8")
    (user / "restart_py.py").write_text(RESTART_PY, encoding="utf-8")
    fx = await boot_flow(tmp_path, node, TOML, store=store_for(tmp_path))
    await start_turn(fx.wire, fx.chat, "t1", command)
    assert await fx.wire.until(lambda: len(HoldVerb.calls) == 1)
    if drain:
        assert fx.chat in await fx.host.drain(0.2)
    await fx.shut()
    HoldVerb.holding = False
    fx2 = await restart_flow(tmp_path, node, TOML)
    try:
        if not drain:
            # A crash resumes nothing by itself: the turn waits, failed and resumable.
            await asyncio.sleep(0.3)
            await resume_by_hand(fx2)
        turn = await wait_resumed(fx2)
        assert final_json(turn) == EXPECTED
        # The finished verb replayed without calling its handler.
        assert len(ECHO_CALLS) == 1
        # The cut-off one was called again, with the same call id.
        assert len(HoldVerb.calls) == 2
        cid = HoldVerb.calls[0]
        assert HoldVerb.calls == [cid, cid]
        assert cid.startswith("verb-")
        lines = notifications(turn)
        assert any(
            line.startswith(
                "resume: wait() #1 was cut off before it finished; "
                "calling its handler again with call id " + cid
            )
            for line in lines
        ), lines

        (resumed,) = found(turn, lambda m: m.get("toolCallId") == "verb-r1-1")
        assert resumed["status"] == "completed", resumed
        (cut,) = found(turn, lambda m: m.get("toolCallId") == "verb-1")
        assert cut["status"] == "cancelled", cut

        state_dir = tmp_path / "state"
        journals = [load_journal(state_dir, d.name) for d in (state_dir / "runs").iterdir()]
        (newest,) = [j for j in journals if j.header.resumed_from is not None]
        assert [(v.ordinal, v.status) for v in newest.verbs] == [(0, "done"), (1, "done")]
        assert state(fx2.host, fx2.chat)["turns"][-1]["id"] == "t1"
    finally:
        await fx2.shut()


@pytest.mark.node
async def test_customverb_restart_drain_js(tmp_path: Path, node_path: str) -> None:
    await _check(tmp_path, node_path, "/restart", drain=True)


@pytest.mark.node
async def test_customverb_restart_crash_js(tmp_path: Path, node_path: str) -> None:
    await _check(tmp_path, node_path, "/restart", drain=False)


async def test_customverb_restart_drain_py(tmp_path: Path) -> None:
    await _check(tmp_path, str(tmp_path / "no-node"), "/restart_py", drain=True)


async def test_customverb_restart_crash_py(tmp_path: Path) -> None:
    await _check(tmp_path, str(tmp_path / "no-node"), "/restart_py", drain=False)
