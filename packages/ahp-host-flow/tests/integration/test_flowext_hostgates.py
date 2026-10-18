"""`ask()`, `approve()` and `canvas()` on the run's main chat, in JS and Python (plan §6)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow import load_config
from ahp_host_flow.inprocess import FlowRunFailed, run_in_process
from tests.flow_fakes import AWS
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json, write_toml
from tests.hosting import all_channels, cancel_turn, found, leaks, start_turn, state, wait_turn

TOML = '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n'
TOKEN = "token=abcdefgh12345678"
RAW_TOKEN = "abcdefgh12345678"

ASK_BODY = (
    "const a = await ask('Ship it?', {choices: ['yes', 'no']})\n"
    "const b = await ask('Name?')\n"
    "const c = await ask('Config', {schema: {type: 'object', properties: "
    "{n: {type: 'integer'}, ok: {type: 'boolean'}}, required: ['n']}})\n"
    "return {a, b, c}\n"
)
ASK_PY = (
    "meta = {'name': 'askflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await ask('Ship it?', choices=['yes', 'no'])\n"
    "    b = await ask('Name?')\n"
    "    c = await ask('Config', schema={'type': 'object', 'properties': "
    "{'n': {'type': 'integer'}, 'ok': {'type': 'boolean'}}, 'required': ['n']})\n"
    "    return {'a': a, 'b': b, 'c': c}\n"
)
APPROVE_BODY = (
    "const a = await approve('Delete branch?', "
    "{diff: [{path: 'a.txt', before: 'x\\n', after: 'y\\n'}]})\n"
    "const b = await approve('Drop table?')\n"
    "return [a, b]\n"
)
APPROVE_PY = (
    "meta = {'name': 'approveflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await approve('Delete branch?', "
    "diff=[{'path': 'a.txt', 'before': 'x\\n', 'after': 'y\\n'}])\n"
    "    b = await approve('Drop table?')\n"
    "    return [a, b]\n"
)
CANVAS_BODY = "await canvas('https://example.com/live', {title: 'Live'})\nreturn 1\n"
CANVAS_PY = (
    "meta = {'name': 'canvasflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    canvas('https://example.com/live', title='Live')\n"
    "    return 1\n"
)


def js(body: str) -> str:
    return "```js\n" + body + "\n```"


def py(base: Path, name: str, source: str) -> str:
    user = base / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / f"{name}.py").write_text(source, encoding="utf-8")
    return f"/{name}"


def needed(fx: FlowFixture) -> list[Mapping[str, Any]]:
    """The session's inputNeeded entries that sit on the run's main chat."""
    entries = state(fx.host, SESSION).get("inputNeeded") or []
    return [e for e in entries if isinstance(e, Mapping) and e.get("chat") == fx.chat]


def seen(fx: FlowFixture, known: set[str]) -> Callable[[], bool]:
    return lambda: any(e["id"] not in known for e in needed(fx))


async def next_entry(fx: FlowFixture, known: set[str]) -> Mapping[str, Any]:
    assert await fx.wire.until(seen(fx, known)), state(fx.host, SESSION)
    entry = next(e for e in needed(fx) if e["id"] not in known)
    known.add(str(entry["id"]))
    return entry


async def answer(fx: FlowFixture, entry: Mapping[str, Any], answers: Mapping[str, Any]) -> None:
    await fx.wire.dispatch(
        fx.chat,
        {
            "type": "chat/inputCompleted",
            "requestId": entry["id"],
            "response": "accept",
            "answers": dict(answers),
        },
    )


def submitted(kind: str, value: Any) -> dict[str, Any]:
    return {"state": "submitted", "value": {"kind": kind, "value": value}}


def questions_of(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(entry["request"].get("questions") or [])


async def decide(fx: FlowFixture, known: set[str], *, approved: bool) -> Mapping[str, Any]:
    """Resolve the next pending confirmation on the main chat; returns the pending call."""
    assert await fx.wire.until(seen(fx, known)), state(fx.host, SESSION)
    entry = next(e for e in needed(fx) if e["id"] not in known)
    known.add(str(entry["id"]))
    call = entry["toolCall"]
    action: dict[str, Any] = {
        "type": "chat/toolCallConfirmed",
        "turnId": entry["turnId"],
        "toolCallId": call["toolCallId"],
        "approved": approved,
        "confirmed": "user-action",
    }
    if not approved:
        action["reason"] = "denied"
    await fx.wire.dispatch(fx.chat, action)
    return dict(call)


async def _ask(tmp_path: Path, node: str, text: str) -> None:
    fx = await boot_flow(tmp_path, node, TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", text)
        known: set[str] = set()
        entry = await next_entry(fx, known)
        (question,) = questions_of(entry)
        assert question["kind"] == "single-select"
        await answer(fx, entry, {"answer": submitted("selected", "yes")})
        entry = await next_entry(fx, known)
        await answer(fx, entry, {"answer": submitted("text", "Ada")})
        entry = await next_entry(fx, known)
        by_id = {q["id"]: q for q in questions_of(entry)}
        assert by_id["n"]["kind"] == "integer"
        assert by_id["n"].get("required") is True
        assert by_id["ok"]["kind"] == "boolean"
        await answer(fx, entry, {"n": submitted("integer", 3), "ok": submitted("boolean", True)})
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {"a": "yes", "b": "Ada", "c": {"n": 3, "ok": True}}
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_hostgates_ask_js(tmp_path: Path, node_path: str) -> None:
    await _ask(tmp_path, node_path, js(ASK_BODY))


async def test_flowext_hostgates_ask_py(tmp_path: Path) -> None:
    await _ask(tmp_path, str(tmp_path / "no-node"), py(tmp_path, "askflow", ASK_PY))


@pytest.mark.node
async def test_flowext_hostgates_ask_reasks_invalid(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        await start_turn(
            fx.wire, fx.chat, "t1", js("return await ask('Ship it?', {choices: ['yes', 'no']})")
        )
        known: set[str] = set()
        entry = await next_entry(fx, known)
        await answer(fx, entry, {"answer": submitted("selected", "maybe")})
        again = await next_entry(fx, known)
        assert str(again["request"]["message"]).startswith("'maybe' is not one of the choices")
        await answer(fx, again, {"answer": submitted("selected", "no")})
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "no"
    finally:
        await fx.shut()


async def _approve(tmp_path: Path, node: str, text: str) -> None:
    fx = await boot_flow(tmp_path, node, TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", text)
        known: set[str] = set()
        first = await decide(fx, known, approved=True)
        assert first["toolName"] == "approve"
        assert first["edits"]
        await decide(fx, known, approved=False)
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == [True, False]
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_hostgates_approve_js(tmp_path: Path, node_path: str) -> None:
    await _approve(tmp_path, node_path, js(APPROVE_BODY))


async def test_flowext_hostgates_approve_py(tmp_path: Path) -> None:
    await _approve(tmp_path, str(tmp_path / "no-node"), py(tmp_path, "approveflow", APPROVE_PY))


@pytest.mark.node
async def test_flowext_hostgates_masking(tmp_path: Path, node_path: str) -> None:
    raw_key = AWS
    # The script is the turn's own text, which the chat keeps verbatim: spell the key out at run
    # time so only the question (not the user message) carries it.
    body = "return await ask('key ' + 'AKIA' + 'TESTONLY'.repeat(2))"
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", js(body))
        known: set[str] = set()
        entry = await next_entry(fx, known)
        assert raw_key not in json.dumps(entry)
        assert leaks(fx.host, raw_key) == []
        await answer(fx, entry, {"answer": submitted("text", TOKEN)})
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "token=[redacted]"
        # The client's own answer stays on the main chat; nothing else carries it.
        others = {
            uri: text
            for uri, text in all_channels(fx.host).items()
            if uri != fx.chat and RAW_TOKEN in text
        }
        assert others == {}
        assert leaks(fx.host, raw_key) == []
        for path in (tmp_path / "state" / "runs").rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace")
                assert raw_key not in content
                assert RAW_TOKEN not in content
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_hostgates_abort(tmp_path: Path, node_path: str) -> None:
    # The withdrawal seen here is the host's own: chat/turnCancelled is reduced before the
    # provider task is cancelled, so it clears inputNeeded and cancels the approve call itself
    # (reason "skipped"), and MainChatGates' later chat/inputCompleted(cancel) and
    # chat/toolCallConfirmed(false, "run aborted") are dropped as no-ops on the cancelled turn.
    # That the gates send them is proved in test_flowext_gateform_abort_withdraws_*.
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        body = "return await parallel([() => ask('Wait?'), () => approve('Go?')])"
        await start_turn(fx.wire, fx.chat, "t1", js(body))
        assert await fx.wire.until(lambda: len({e["id"] for e in needed(fx)}) == 2), state(
            fx.host, SESSION
        )
        await cancel_turn(fx.wire, fx.chat, "t1")
        assert await fx.wire.until(lambda: not needed(fx)), state(fx.host, SESSION)
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert turn["state"] == "cancelled"
        (call,) = found(turn, lambda m: m.get("toolName") == "approve")
        assert call["status"] == "cancelled"
        # Cancelled by the host's reduction, not by the provider's "run aborted" denial.
        assert call.get("reason") == "skipped"
        (part,) = [p for p in turn["responseParts"] if p.get("kind") == "inputRequest"]
        assert part["request"]["questions"][0]["message"] == "Wait?"
        # Never answered: no response was recorded on the withdrawn request.
        assert "response" not in part
        assert "answers" not in part
        assert needed(fx) == []
    finally:
        await fx.shut()


async def _canvas(tmp_path: Path, node: str, text: str) -> None:
    fx = await boot_flow(tmp_path, node, TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", text)
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == 1
        (reference,) = state(fx.host, fx.chat)["canvases"]
        canvas = state(fx.host, str(reference["resource"]))
        assert canvas["url"] == "https://example.com/live"
        assert canvas["title"] == "Live"
        assert canvas["extensionId"] == "ahp-py/flow"
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_hostgates_canvas_js(tmp_path: Path, node_path: str) -> None:
    await _canvas(tmp_path, node_path, js(CANVAS_BODY))


async def test_flowext_hostgates_canvas_py(tmp_path: Path) -> None:
    await _canvas(tmp_path, str(tmp_path / "no-node"), py(tmp_path, "canvasflow", CANVAS_PY))


async def _headless(tmp_path: Path, node: str, source: str, filename: str) -> None:
    config = load_config(write_toml(tmp_path, node, TOML))
    with pytest.raises(FlowRunFailed) as info:
        await asyncio.wait_for(
            run_in_process(config, source, root=tmp_path, filename=filename), timeout=30
        )
    assert str(info.value).startswith("the workflow's ask() gate needs an answer: Proceed?")


@pytest.mark.node
async def test_flowext_hostgates_headless_js(tmp_path: Path, node_path: str) -> None:
    await _headless(tmp_path, node_path, "return await ask('Proceed?')", "g.js")


async def test_flowext_hostgates_headless_py(tmp_path: Path) -> None:
    source = "async def main(args):" + chr(10) + "    return await ask('Proceed?')" + chr(10)
    await _headless(tmp_path, str(tmp_path / "no-node"), source, "g.py")
