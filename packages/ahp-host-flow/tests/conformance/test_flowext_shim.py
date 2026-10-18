"""`shim/flow-shim.js`: one script under a Claude-Code-like harness and under ahp-host-flow."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests import flow_fakes
from tests.flow_fakes import RecordingProvider
from tests.flow_hosting import boot_flow, final_json
from tests.hosting import start_turn, state, wait_turn
from tests.integration.test_flowext_hostgates import answer, decide, next_entry, submitted

ROOT = Path(__file__).resolve().parents[2]
SHIM = ROOT / "shim" / "flow-shim.js"
DUAL = ROOT / "conformance" / "shim" / "dual_target.js"
HARNESS = Path(__file__).with_name("cc_harness.mjs")
ANSWERS = {
    "by_label": {"plan": "P", "ask": {"answer": "yes"}, "approve": {"approved": True}, "apply": "D"}
}


async def cc(
    node: str, script_text: str, answers: dict[str, Any], tmp_path: Path
) -> dict[str, Any]:
    script = tmp_path / "script.js"
    answers_file = tmp_path / "answers.json"
    script.write_text(script_text, encoding="utf-8")
    answers_file.write_text(json.dumps(answers), encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        node,
        str(HARNESS),
        str(script),
        str(answers_file),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _err = await asyncio.wait_for(proc.communicate(), 30)
    result: dict[str, Any] = json.loads(out.decode("utf-8").strip().splitlines()[-1])
    return result


def test_flowext_shim_text_matches() -> None:
    shim = SHIM.read_text(encoding="utf-8")
    assert shim in DUAL.read_text(encoding="utf-8")


@pytest.mark.node
async def test_flowext_shim_cc_runs(tmp_path: Path, node_path: str) -> None:
    res = await cc(node_path, DUAL.read_text(encoding="utf-8"), ANSWERS, tmp_path)
    assert res["ok"] is True, res
    assert res["value"] == {"plan": "P", "go": "yes", "ok": True, "done": "D"}
    assert [c["opts"].get("label") for c in res["calls"]] == ["plan", "ask", "approve", "apply"]
    assert res["calls"][0]["opts"]["x"] == {"chat": "planner"}


@pytest.mark.node
async def test_flowext_shim_cc_without_shim_fails(tmp_path: Path, node_path: str) -> None:
    text = DUAL.read_text(encoding="utf-8").replace(SHIM.read_text(encoding="utf-8"), "")
    res = await cc(node_path, text, ANSWERS, tmp_path)
    assert res["ok"] is False, res
    assert "ask is not defined" in res["error"]


@pytest.mark.node
async def test_flowext_shim_cc_default(tmp_path: Path, node_path: str) -> None:
    text = DUAL.read_text(encoding="utf-8").replace(
        "approveDefault: undefined", "approveDefault: false"
    )
    res = await cc(node_path, text, ANSWERS, tmp_path)
    assert res["ok"] is True, res
    assert res["value"] == {"plan": "P", "go": "yes", "ok": False, "done": None}
    assert "approve" not in [c["opts"].get("label") for c in res["calls"]]


@pytest.mark.node
async def test_flowext_shim_ahp_host_flow(tmp_path: Path, node_path: str) -> None:
    flow_fakes.reset_fakes()
    try:
        toml = '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n'
        fx = await boot_flow(tmp_path, node_path, toml)
        try:
            text = "```js\n" + DUAL.read_text(encoding="utf-8") + "\n```"
            await start_turn(fx.wire, fx.chat, "t1", text)
            known: set[str] = set()
            entry = await next_entry(fx, known)
            await answer(fx, entry, {"answer": submitted("selected", "yes")})
            await decide(fx, known, approved=True)
            turn = await wait_turn(fx.wire, fx.chat, "t1")
            assert final_json(turn) == {
                "plan": "rec: plan the change",
                "go": "yes",
                "ok": True,
                "done": "rec: apply the plan",
            }
            (ref,) = state(fx.host, fx.chat)["canvases"]
            assert state(fx.host, ref["resource"])["url"] == "https://example.com/run"
            (rec,) = RecordingProvider.instances
            assert len(rec.sessions) == 1
            assert [m.text for m in rec.sessions[0].messages] == [
                "plan the change",
                "apply the plan",
            ]
        finally:
            await fx.shut()
    finally:
        flow_fakes.reset_fakes()
