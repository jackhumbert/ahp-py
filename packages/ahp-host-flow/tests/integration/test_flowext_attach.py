"""`opts.x.attach` hands a call files and prior results as message attachments."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import flow_fakes
from tests.flow_fakes import RecordingProvider
from tests.flow_hosting import boot_flow, final_json
from tests.hosting import notifications, run_turn

TOML = (
    '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n\n[routing]\ndefault = "rec"\n'
)
OUTSIDE = "outside the run's working directories"
JS = (
    "const prior = await agent('first'); "
    "return await agent('use them', {x: {attach: [{kind: 'file', path: 'notes.txt'}, "
    "{kind: 'result', value: {prior}, label: 'prior'}]}})"
)
PY = (
    "meta = {'name': 'att', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    prior = await agent('first')\n"
    "    return await agent('use them', x={'attach': [\n"
    "        {'kind': 'file', 'path': 'notes.txt'},\n"
    "        {'kind': 'result', 'value': {'prior': prior}, 'label': 'prior'},\n"
    "    ]})\n"
)


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    flow_fakes.reset_fakes()
    yield
    flow_fakes.reset_fakes()


def js(body: str) -> str:
    return "```js\n" + body + "\n```"


def write_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("no", encoding="utf-8")


def sent_texts() -> list[str]:
    return [m.text for p in RecordingProvider.instances for s in p.sessions for m in s.messages]


async def _attach(tmp_path: Path, node: str, text: str) -> None:
    write_files(tmp_path)
    fx = await boot_flow(tmp_path, node, TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", text)
        assert final_json(turn) == "rec: use them"
        (msg,) = (
            m
            for p in RecordingProvider.instances
            for s in p.sessions
            for m in s.messages
            if m.text == "use them"
        )
        attachments = msg.raw["attachments"]
        assert len(attachments) == 2
        assert attachments[0] == {
            "type": "resource",
            "uri": (fx.workspace / "notes.txt").resolve().as_uri(),
            "label": "notes.txt",
            "contentType": "text/plain",
        }
        assert attachments[1]["type"] == "embeddedResource"
        assert attachments[1]["label"] == "prior"
        assert attachments[1]["contentType"] == "application/json"
        assert json.loads(base64.b64decode(attachments[1]["data"])) == {"prior": "rec: first"}
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_attach_js(tmp_path: Path, node_path: str) -> None:
    await _attach(tmp_path, node_path, js(JS))


async def test_flowext_attach_py(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "att.py").write_text(PY, encoding="utf-8")
    await _attach(tmp_path, str(tmp_path / "no-node"), "/att")


@pytest.mark.node
async def test_flowext_attach_confined(tmp_path: Path, node_path: str) -> None:
    write_files(tmp_path)
    paths = ["../outside.txt", str(tmp_path / "outside.txt"), "missing.txt"]
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        try:
            (fx.workspace / "link.txt").symlink_to(tmp_path / "outside.txt")
            paths.append("link.txt")
        except OSError:
            pass
        body = (
            f"const r = []; for (const p of {json.dumps(paths)}) "
            "r.push(await agent('attach ' + p, {x: {attach: [{kind: 'file', path: p}]}})); "
            "return r"
        )
        turn = await run_turn(fx.wire, fx.chat, "t1", js(body))
        assert final_json(turn) == [None] * len(paths)
        notes = notifications(turn)
        for p in paths:
            if p != "missing.txt":
                assert any(f"{p} is {OUTSIDE}" in n for n in notes), p
        assert any("missing.txt does not exist" in n for n in notes)
        assert not any(t.startswith("attach") for t in sent_texts())
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_attach_bad_shape(tmp_path: Path, node_path: str) -> None:
    write_files(tmp_path)
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        body = (
            "const a = await agent('a', {x: {attach: 'notes.txt'}}); "
            "const b = await agent('b', {x: {attach: [{kind: 'url'}]}}); "
            "return [a, b]"
        )
        turn = await run_turn(fx.wire, fx.chat, "t1", js(body))
        assert final_json(turn) == [None, None]
        notes = notifications(turn)
        assert any("opts.x.attach must be an array" in n for n in notes)
        assert any("opts.x.attach[0]: kind must be 'file' or 'result'" in n for n in notes)
    finally:
        await fx.shut()
