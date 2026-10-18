"""A custom verb's `ask` and `confirm` reach the human on the run's main chat (M12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.flow_hosting import SESSION, boot_flow, final_json
from tests.hosting import start_turn, wait_turn
from tests.integration.test_flowext_hostgates import (
    answer,
    decide,
    js,
    next_entry,
    py,
    questions_of,
    submitted,
)

TOML = (
    '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n\n'
    "[verbs]\n"
    'pick = "tests.hook_fakes:asking_verb"\n'
    'sure = "tests.hook_fakes:confirming_verb"\n'
    'badask = "tests.hook_fakes:bad_ask_verb"\n'
)

ASK_JS = "return await pick()\n"
ASK_PY = (
    "meta = {'name': 'pickflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await pick()\n"
)
CONFIRM_PY = (
    "meta = {'name': 'sureflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return [await sure(), await sure()]\n"
)
BAD_PY = (
    "meta = {'name': 'badflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    try:\n"
    "        await badask()\n"
    "    except Exception as exc:\n"
    "        return str(exc)\n"
)


async def _ask(tmp_path: Path, node: str, text: str) -> None:
    fx = await boot_flow(tmp_path, node, TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", text)
        known: set[str] = set()
        entry = await next_entry(fx, known)
        (question,) = questions_of(entry)
        assert question["kind"] == "single-select"
        assert [o["id"] for o in question["options"]] == ["red", "blue"]
        await answer(fx, entry, {"answer": submitted("selected", "blue")})
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {"answer": "blue", "session": SESSION, "chat": fx.chat}
    finally:
        await fx.shut()


@pytest.mark.node
async def test_customverb_context_ask_js(tmp_path: Path, node_path: str) -> None:
    await _ask(tmp_path, node_path, js(ASK_JS))


async def test_customverb_context_ask_py(tmp_path: Path) -> None:
    await _ask(tmp_path, str(tmp_path / "no-node"), py(tmp_path, "pickflow", ASK_PY))


async def test_customverb_context_confirm(tmp_path: Path) -> None:
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "sureflow", CONFIRM_PY))
        known: set[str] = set()
        first = await decide(fx, known, approved=True)
        assert first["toolName"] == "approve"
        second = await decide(fx, known, approved=False)
        assert second["toolName"] == "approve"
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == [True, False]
    finally:
        await fx.shut()


async def test_customverb_context_bad_options(tmp_path: Path) -> None:
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "badflow", BAD_PY))
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "ask() choices must be a non-empty array of strings"
    finally:
        await fx.shut()
