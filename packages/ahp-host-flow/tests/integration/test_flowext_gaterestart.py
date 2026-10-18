"""An open `ask()`/`approve()` gate cut off by a host restart, in JS and Python (plan §6, §4.8).

The restart withdraws the open request with a notice (ahp-host's stored-request
handling), `resume_turn` continues the run, answered gates replay from the
journal and the open one is asked again under the resumed turn.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import load_journal
from tests.flow_fakes import RestartHoldProvider, reset_fakes
from tests.flow_hosting import (
    SESSION,
    FlowFixture,
    boot_flow,
    final_json,
    restart_flow,
    store_for,
)
from tests.hosting import found, start_turn, state
from tests.integration.test_flow_durability import resume_by_hand, wait_resumed

TOML = (
    '[providers.hold]\nload = "tests.flow_fakes:RestartHoldProvider"\n'
    '\n[routing]\ndefault = "hold"\n'
)
GATED_JS = (
    "const a = await agent('one')\n"
    "const g = await ask('Proceed?', {choices: ['go', 'stop']})\n"
    "const h = await ask('Second?')\n"
    "return {a, g, h}\n"
)
GATED_PY = (
    "meta = {'name': 'gated_py', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await agent('one')\n"
    "    g = await ask('Proceed?', choices=['go', 'stop'])\n"
    "    h = await ask('Second?')\n"
    "    return {'a': a, 'g': g, 'h': h}\n"
)
APPROVE_JS = "const ok = await approve('Deploy?')\nreturn ok\n"
EXPECTED = {"a": "rec: one", "g": "go", "h": "yes"}
WAIT = 10.0


def write_workflows(base: Path) -> None:
    user = base / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "gated.js").write_text(GATED_JS, encoding="utf-8")
    (user / "gated_py.py").write_text(GATED_PY, encoding="utf-8")
    (user / "deploy.js").write_text(APPROVE_JS, encoding="utf-8")


def asking(fx: FlowFixture, text: str) -> list[Mapping[str, Any]]:
    """The inputNeeded entries on the main chat whose request mentions *text*."""
    entries = state(fx.host, SESSION).get("inputNeeded") or []
    return [
        e
        for e in entries
        if isinstance(e, Mapping) and e.get("chat") == fx.chat and text in json.dumps(e)
    ]


async def wait_entry(fx: FlowFixture, text: str, *, not_id: str | None = None) -> Mapping[str, Any]:
    def ready() -> bool:
        return any(e["id"] != not_id for e in asking(fx, text))

    assert await fx.wire.until(ready, timeout=WAIT), state(fx.host, SESSION)
    return next(e for e in asking(fx, text) if e["id"] != not_id)


async def answer(fx: FlowFixture, entry: Mapping[str, Any], kind: str, value: Any) -> None:
    await fx.wire.dispatch(
        fx.chat,
        {
            "type": "chat/inputCompleted",
            "requestId": entry["id"],
            "response": "accept",
            "answers": {"answer": {"state": "submitted", "value": {"kind": kind, "value": value}}},
        },
    )


async def cut_off_at(tmp_path: Path, node: str, command: str, text: str, *, drain: bool) -> str:
    """Run *command* until the gate mentioning *text* is open, stop the host; returns its id."""
    reset_fakes()
    write_workflows(tmp_path)
    fx = await boot_flow(tmp_path, node, TOML, store=store_for(tmp_path))
    await start_turn(fx.wire, fx.chat, "t1", command)
    if text == "Second?":
        await answer(fx, await wait_entry(fx, "Proceed?"), "selected", "go")
    entry = await wait_entry(fx, text)
    if drain:
        assert fx.chat in await fx.host.drain(0.2)
    await fx.shut()
    return str(entry["id"])


def withdrawals(fx: FlowFixture) -> list[Mapping[str, Any]]:
    return found(
        state(fx.host, fx.chat),
        lambda m: (
            m.get("kind") == "systemNotification"
            and isinstance(m.get("_meta"), Mapping)
            and "ahp-py/restart" in m["_meta"]
        ),
    )


async def _check(tmp_path: Path, node: str, command: str, *, drain: bool) -> None:
    old_id = await cut_off_at(tmp_path, node, command, "Second?", drain=drain)
    fx = await restart_flow(tmp_path, node, TOML)
    try:
        if not drain:
            # A crash resumes nothing by itself: the client asks.
            await resume_by_hand(fx)
        entry = await wait_entry(fx, "Second?", not_id=old_id)
        assert entry["id"] != old_id
        assert asking(fx, "Proceed?") == []
        notices = withdrawals(fx)
        assert any(old_id in n["_meta"]["ahp-py/restart"].get("requests", []) for n in notices), (
            notices
        )
        await answer(fx, entry, "text", "yes")
        turn = await wait_resumed(fx)
        assert final_json(turn) == EXPECTED
        assert asking(fx, "Proceed?") == []
        assert RestartHoldProvider.prompts == ["one"]
        runs = sorted((tmp_path / "state" / "runs").iterdir(), key=lambda p: p.stat().st_mtime_ns)
        gates = load_journal(tmp_path / "state", runs[-1].name).gates
        assert [(g.ordinal, g.status, g.answer) for g in gates] == [
            (0, "answered", "go"),
            (1, "answered", "yes"),
        ]
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_gaterestart_drain_js(tmp_path: Path, node_path: str) -> None:
    await _check(tmp_path, node_path, "/gated", drain=True)


async def test_flowext_gaterestart_drain_py(tmp_path: Path) -> None:
    await _check(tmp_path, str(tmp_path / "no-node"), "/gated_py", drain=True)


@pytest.mark.node
async def test_flowext_gaterestart_crash_js(tmp_path: Path, node_path: str) -> None:
    await _check(tmp_path, node_path, "/gated", drain=False)


async def test_flowext_gaterestart_crash_py(tmp_path: Path) -> None:
    await _check(tmp_path, str(tmp_path / "no-node"), "/gated_py", drain=False)


@pytest.mark.node
async def test_flowext_gaterestart_approve_reraised(tmp_path: Path, node_path: str) -> None:
    old_id = await cut_off_at(tmp_path, node_path, "/deploy", "Deploy?", drain=True)
    fx = await restart_flow(tmp_path, node_path, TOML)
    try:
        entry = await wait_entry(fx, "Deploy?", not_id=old_id)
        call = entry["toolCall"]
        assert str(call["toolCallId"]).startswith("gate-r1-"), call
        await fx.wire.dispatch(
            fx.chat,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": entry["turnId"],
                "toolCallId": call["toolCallId"],
                "approved": True,
                "confirmed": "user-action",
            },
        )
        turn = await wait_resumed(fx)
        assert final_json(turn) is True
    finally:
        await fx.shut()
