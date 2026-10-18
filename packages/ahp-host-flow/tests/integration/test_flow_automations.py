"""Automations run workflows unattended: '/<workflow> <json args>' as the saved message (plan §4.3).

A workflow's own default schedule (`meta.x.schedule`) is M10, plan §6; here the
trigger is a manual run, the same machinery a cron trigger uses.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ahp_host.core.automations import InMemoryAutomationStore
from ahp_protocol.channels import AUTOMATIONS_URI

from tests.flow_fakes import reset_fakes
from tests.flow_hosting import FlowFixture, boot_flow, descriptors, final_json
from tests.hosting import notifications, state

pytestmark = pytest.mark.node

AUTOMATION = "ahp-automation:/nightly"
PLUGIN = "virtual://my-client/house"

TOML = """
[providers.rec]
load = "tests.flow_fakes:RecordingProvider"

[routing]
default = "rec"
"""

WITH_CLIENT = TOML + "\n[workflows]\nsources = ['client', 'workspace', 'user', 'host']\n"

NIGHTLY = (
    "export const meta = {name:'nightly', description:'nightly check', phases:[{title:'Check'}]}\n"
    "phase('Check')\n"
    "const r = await agent(`check ${args.target}`, {label:'check'})\n"
    "return {target: args.target, r}\n"
)
FROM_CLIENT = (
    "export const meta = {name:'nightly', description:'from the client', phases:[]}\n"
    "return {from:'client', target: args.target}\n"
)


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def definition(fx: FlowFixture, text: str, **session: Any) -> dict[str, Any]:
    return {
        "title": "Nightly",
        "message": {"text": text, "origin": {"kind": "automation"}},
        "session": {
            "provider": "flow",
            "workingDirectories": [fx.workspace.as_uri()],
            **session,
        },
        "enabled": True,
        "triggers": [],
    }


def entries(fx: FlowFixture) -> list[dict[str, Any]]:
    return list(state(fx.host, AUTOMATIONS_URI)["entries"])


async def save(fx: FlowFixture, saved: dict[str, Any]) -> None:
    await fx.wire.request("subscribe", {"channel": AUTOMATIONS_URI})
    await fx.wire.dispatch(
        AUTOMATIONS_URI,
        {"type": "automation/createRequested", "resource": AUTOMATION, "definition": saved},
    )
    assert await fx.wire.until(lambda: any(e["resource"] == AUTOMATION for e in entries(fx)))


async def run_to_end(fx: FlowFixture) -> dict[str, Any]:
    """Run the automation by hand; the finished turn of the run's session."""
    reply = await fx.wire.request(
        "runAutomation", {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r1"}
    )
    run = reply["result"]["resource"]

    def completed() -> bool:
        return str(state(fx.host, run)["lifecycle"]["status"]) == "completed"

    assert await fx.wire.until(completed), state(fx.host, run)
    session = str(state(fx.host, run)["primarySession"])
    chat = str(state(fx.host, session)["defaultChat"])
    (turn,) = state(fx.host, chat)["turns"]
    return dict(turn)


async def test_flow_automations_manual_run(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, TOML, automations=InMemoryAutomationStore())
    try:
        (fx.workspace / ".claude" / "workflows" / "nightly.js").write_text(
            NIGHTLY, encoding="utf-8"
        )
        await save(fx, definition(fx, '/nightly {"target": "docs"}'))
        turn = await run_to_end(fx)
        assert final_json(turn) == {"target": "docs", "r": "rec: check docs"}
        found = descriptors(turn)
        assert any("runStarted" in d for d in found)
        finished = [d["runFinished"] for d in found if "runFinished" in d]
        assert [f["status"] for f in finished] == ["ok"]
        (run_id,) = [n[len("Run ") :] for n in notifications(turn) if n.startswith("Run ")]
        assert (tmp_path / "state" / "runs" / run_id / "journal.jsonl").is_file()
    finally:
        await fx.shut()


async def test_flow_automations_client_workflow_captured(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, WITH_CLIENT, automations=InMemoryAutomationStore())
    try:
        fx.wire.resources[f"{PLUGIN}/workflows/nightly.js"] = FROM_CLIENT
        customizations = [
            {"type": "plugin", "id": "house", "uri": PLUGIN, "name": "House", "nonce": "n1"}
        ]
        await save(
            fx,
            definition(fx, '/client:nightly {"target": "copy"}', customizations=customizations),
        )
        # The host captured the plugin at save time; the client can no longer serve it.
        served = len(fx.wire.served)
        fx.wire.resources.clear()
        turn = await run_to_end(fx)
        assert final_json(turn) == {"from": "client", "target": "copy"}
        assert len(fx.wire.served) == served
    finally:
        await fx.shut()
