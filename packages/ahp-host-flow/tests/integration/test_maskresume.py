"""A masked journal never loses a run: a restart resumes it from the real script (M17 item 4).

The journal holds the script and args masked, with their sha256. After a restart the provider
reads the real script back by re-resolving the run's workflow, and the real args by re-parsing
the turn's own message from the session store, and checks both against the hashes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tests.flow_fakes import AWS, GH, RestartHoldProvider, reset_fakes
from tests.flow_hosting import FlowFixture, boot_flow, final_json, restart_flow, store_for
from tests.hosting import start_turn, state
from tests.integration.test_flow_durability import resume_by_hand, wait_resumed

NO_NODE = "no-node"
TOML = (
    '[providers.hold]\nload = "tests.flow_fakes:RestartHoldProvider"\n\n'
    '[routing]\ndefault = "hold"\n'
)
WORKFLOW = (
    "meta = {'name': 'masked', 'description': 'd'}\n"
    f'# deploy token = "{GH}"\n'
    "\n"
    "async def main(args):\n"
    "    a = await agent('one ' + args['k'])\n"
    "    b = await agent('hold two')\n"
    "    return {'a': a, 'b': b, 'k': args['k']}\n"
)
COMMAND = "/masked " + json.dumps({"k": "hi", "key": AWS})
RESULT = {"a": "rec: one hi", "b": "rec: hold two", "k": "hi"}


def _journals(base: Path) -> str:
    runs = base / "state" / "runs"
    return "".join(p.read_text(encoding="utf-8") for p in sorted(runs.glob("*/journal.jsonl")))


async def _cut_off(base: Path, *, drain: bool) -> None:
    reset_fakes()
    user = base / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "masked.py").write_text(WORKFLOW, encoding="utf-8")
    fx = await boot_flow(base, str(base / NO_NODE), TOML, store=store_for(base))
    await start_turn(fx.wire, fx.chat, "t1", COMMAND)
    assert await fx.wire.until(lambda: "hold two" in RestartHoldProvider.prompts, timeout=10)
    if drain:
        assert fx.chat in await fx.host.drain(0.2)
    await fx.shut()
    text = _journals(base)
    assert '"sourceSha"' in text
    assert GH not in text
    assert AWS not in text


async def _restarted(base: Path, *, drain: bool) -> FlowFixture:
    RestartHoldProvider.holding = False
    fx = await restart_flow(base, str(base / NO_NODE), TOML)
    if not drain:
        # A crash resumes nothing by itself: a client resumes the failed turn.
        await asyncio.sleep(0.3)
        await resume_by_hand(fx)
    return fx


async def _resumes(base: Path, *, drain: bool) -> None:
    await _cut_off(base, drain=drain)
    fx = await _restarted(base, drain=drain)
    try:
        turn = await wait_resumed(fx)
        assert final_json(turn) == RESULT
        # `one hi` replayed from the journal; only the cut-off call ran again.
        assert RestartHoldProvider.prompts == ["one hi", "hold two", "hold two"]
        text = _journals(base)
        assert len(list((base / "state" / "runs").glob("*/journal.jsonl"))) == 2
        assert GH not in text
        assert AWS not in text
    finally:
        await fx.shut()


async def test_maskresume_resumes_after_drain(tmp_path: Path) -> None:
    await _resumes(tmp_path, drain=True)


async def test_maskresume_resumes_after_crash(tmp_path: Path) -> None:
    await _resumes(tmp_path, drain=False)


def _error_messages(turn: Mapping[str, Any]) -> list[str]:
    return [
        str(part["error"].get("message", ""))
        for part in turn.get("responseParts", [])
        if isinstance(part, Mapping) and isinstance(part.get("error"), Mapping)
    ]


async def test_maskresume_changed_script_fails_with_the_line(tmp_path: Path) -> None:
    await _cut_off(tmp_path, drain=True)
    (tmp_path / "user" / "masked.py").write_text(WORKFLOW + "# edited\n", encoding="utf-8")
    fx = await _restarted(tmp_path, drain=True)
    try:

        def failed_again() -> list[str] | None:
            turns = state(fx.host, fx.chat).get("turns", [])
            last = turns[-1] if turns else None
            if not isinstance(last, Mapping) or state(fx.host, fx.chat).get("activeTurn"):
                return None
            errors = _error_messages(last)
            return errors if len(errors) > 1 else None

        assert await fx.wire.until(lambda: failed_again() is not None, timeout=20)
        errors = failed_again()
        assert errors is not None
        assert "the workflow changed since the run started" in errors[-1]
        # Nothing ran a different script: no call was sent again.
        assert RestartHoldProvider.prompts == ["one hi", "hold two"]
    finally:
        await fx.shut()


async def test_maskresume_changed_args_fail_with_the_line(tmp_path: Path) -> None:
    """The stored message no longer carries the args the journal hashed (redacted, say)."""
    await _cut_off(tmp_path, drain=True)
    sessions = tmp_path / "sessions"
    for path in sessions.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(AWS, "AKIA" + "OTHERKEY" * 2), encoding="utf-8")
    fx = await _restarted(tmp_path, drain=True)
    try:

        def failed_again() -> list[str] | None:
            turns = state(fx.host, fx.chat).get("turns", [])
            last = turns[-1] if turns else None
            if not isinstance(last, Mapping) or state(fx.host, fx.chat).get("activeTurn"):
                return None
            errors = _error_messages(last)
            return errors if len(errors) > 1 else None

        assert await fx.wire.until(lambda: failed_again() is not None, timeout=20)
        errors = failed_again()
        assert errors is not None
        assert "the args changed since the run started" in errors[-1]
        assert RestartHoldProvider.prompts == ["one hi", "hold two"]
    finally:
        await fx.shut()
