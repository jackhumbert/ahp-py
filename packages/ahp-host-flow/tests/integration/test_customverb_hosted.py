"""Custom verbs on the run's main chat: tool calls, progress, masking and the journal (M12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.ext import flow

from ahp_host_flow.engine import load_journal
from tests.flow_fakes import AWS
from tests.flow_hosting import SESSION, boot_flow, descriptors, final_json
from tests.hosting import found, leaks, start_turn, state, wait_turn

TOML = (
    '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n\n'
    '[verbs]\ntool = "tests.hook_fakes:echo_verb"\nboom = "tests.hook_fakes:fail_verb"\n'
)
APPROVE_TOML = (
    '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n\n'
    '[verbs]\napprove = "tests.hook_fakes:echo_verb"\n'
)
NO_VERBS_TOML = '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n'

# The key is spelled out at run time: the script is the turn's own text, which the chat keeps.
FLOW_JS = (
    "phase('Build')\n"
    "const t = await tool('x', 'AKIA' + 'TESTONLY'.repeat(2))\n"
    "let e = null\n"
    "try { await boom(1) } catch (x) { e = x.message }\n"
    "const a = await agent('a')\n"
    "return {t, e}\n"
)
FLOW_PY = (
    "meta = {'name': 'verbflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    phase('Build')\n"
    "    t = await tool('x', 'AKIA' + 'TESTONLY' * 2)\n"
    "    e = None\n"
    "    try:\n"
    "        await boom(1)\n"
    "    except Exception as exc:\n"
    "        e = str(exc)\n"
    "    a = await agent('a')\n"
    "    return {'t': t, 'e': e}\n"
)
APPROVE_JS = "return await approve('ada', 'Ship?')\n"
APPROVE_PY = (
    "meta = {'name': 'approveverb', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await approve('ada', 'Ship?')\n"
)
UNDEFINED_PY = (
    "meta = {'name': 'undefinedverb', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return 'tool' not in globals()\n"
)


def js(body: str) -> str:
    return "```js\n" + body + "\n```"


def py(base: Path, name: str, source: str) -> str:
    user = base / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / f"{name}.py").write_text(source, encoding="utf-8")
    return f"/{name}"


def call_of(turn: Any, call_id: str) -> Any:
    (call,) = found(turn, lambda m: m.get("toolCallId") == call_id)
    return call


def finished_of(call: Any) -> Any:
    (descriptor,) = [d for d in descriptors(call) if "verbFinished" in d]
    return descriptor


async def _hosted(tmp_path: Path, node: str, text: str) -> None:
    fx = await boot_flow(tmp_path, node, TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", text)
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {
            "t": {"name": "tool", "args": ["x", "[redacted]"]},
            "e": "boom failed: 1",
        }

        first = call_of(turn, "verb-0")
        assert first["toolName"] == "tool"
        assert first["status"] == "completed"
        done = finished_of(first)
        assert done["verbStarted"] == {"name": "tool", "ordinal": 0, "phase": "Build"}
        assert done["verbFinished"] == {"status": "ok"}

        second = call_of(turn, "verb-1")
        assert second["toolName"] == "boom"
        failed = finished_of(second)
        assert failed["verbFinished"] == {"status": "error", "error": "boom failed: 1"}

        view = flow.run_view(state(fx.host, fx.chat))
        assert [(v.ordinal, v.name, v.status, v.phase, v.kind) for v in view.verbs] == [
            (0, "tool", "ok", "Build", "verb"),
            (1, "boom", "error", "Build", "verb"),
        ]
        assert len(view.steps) == 1
        assert leaks(fx.host, AWS) == []

        (run,) = (tmp_path / "state" / "runs").iterdir()
        records = {v.ordinal: v for v in load_journal(tmp_path / "state", run.name).verbs}
        assert (records[0].status, records[0].name) == ("done", "tool")
        assert records[0].value == {"name": "tool", "args": ["x", "[redacted]"]}
        assert (records[1].status, records[1].error) == ("error", "boom failed: 1")
        for path in (tmp_path / "state" / "runs").rglob("*"):
            if path.is_file():
                assert AWS not in path.read_text(encoding="utf-8", errors="replace")
    finally:
        await fx.shut()


@pytest.mark.node
async def test_customverb_hosted_js(tmp_path: Path, node_path: str) -> None:
    await _hosted(tmp_path, node_path, js(FLOW_JS))


async def test_customverb_hosted_py(tmp_path: Path) -> None:
    await _hosted(tmp_path, str(tmp_path / "no-node"), py(tmp_path, "verbflow", FLOW_PY))


async def _replaced_approve(tmp_path: Path, node: str, text: str) -> None:
    fx = await boot_flow(tmp_path, node, APPROVE_TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", text)
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {"name": "approve", "args": ["ada", "Ship?"]}
        assert not state(fx.host, SESSION).get("inputNeeded")
        assert not found(turn, lambda m: m.get("kind") == "inputRequest")
        assert not found(turn, lambda m: m.get("status") == "pending-confirmation")
    finally:
        await fx.shut()


@pytest.mark.node
async def test_customverb_hosted_replaced_approve_js(tmp_path: Path, node_path: str) -> None:
    await _replaced_approve(tmp_path, node_path, js(APPROVE_JS))


async def test_customverb_hosted_replaced_approve_py(tmp_path: Path) -> None:
    await _replaced_approve(
        tmp_path, str(tmp_path / "no-node"), py(tmp_path, "approveverb", APPROVE_PY)
    )


async def test_customverb_hosted_undefined_verb(tmp_path: Path) -> None:
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), NO_VERBS_TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "undefinedverb", UNDEFINED_PY))
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) is True
    finally:
        await fx.shut()
