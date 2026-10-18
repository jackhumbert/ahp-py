"""Workflows a client publishes in a plugin (plan §4.3): listed, run, republished live."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.flow_fakes import reset_fakes
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import run_turn, state, text_of

pytestmark = pytest.mark.node

PLUGIN = "virtual://my-client/house"
CONTAINER = "ahp-host-flow.client.house"

TOML = """
[providers.rec]
load = "tests.flow_fakes:RecordingProvider"

[routing]
default = "rec"
"""

WITH_CLIENT = TOML + "\n[workflows]\nsources = ['client', 'workspace', 'user', 'host']\n"

NIGHTLY = (
    "export const meta = {name:'nightly', description:'from the client', phases:[]}\n"
    "return {from: 'client', r: await agent('client hi', {label:'c'})}\n"
)
SECOND = "export const meta = {name:'second', description:'another', phases:[]}\nreturn 2\n"
EVIL = "export const meta = {name:'evil', description:'x', phases:[]}\nreturn 1\n"


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def plugin_entry(nonce: str) -> dict[str, Any]:
    return {"type": "plugin", "id": "house", "uri": PLUGIN, "name": "House", "nonce": nonce}


async def publish(fx: FlowFixture, customizations: list[dict[str, Any]]) -> None:
    await fx.wire.dispatch(
        SESSION,
        {
            "type": "session/activeClientSet",
            "activeClient": {"clientId": "c1", "tools": [], "customizations": customizations},
        },
    )


def containers(fx: FlowFixture) -> dict[str, dict[str, Any]]:
    return {str(c["id"]): c for c in state(fx.host, SESSION).get("customizations", [])}


def children(fx: FlowFixture, container: str) -> list[str]:
    found = containers(fx).get(container)
    return [str(c["name"]) for c in (found or {}).get("children", [])]


async def boot(tmp_path: Path, node_path: str, toml: str) -> FlowFixture:
    fx = await boot_flow(tmp_path, node_path, toml)
    fx.wire.resources[f"{PLUGIN}/workflows/nightly.js"] = NIGHTLY
    fx.wire.resources[f"{PLUGIN}/workflows/evil.py"] = "return 1\n"
    (fx.workspace / ".claude" / "workflows" / "nightly.js").write_text(
        "export const meta = {name:'nightly', description:'ws', phases:[]}\n"
        "return {from: 'workspace'}\n",
        encoding="utf-8",
    )
    return fx


async def test_client_workflows_listed_and_run(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path, WITH_CLIENT)
    try:
        await publish(fx, [plugin_entry("n1")])
        assert await fx.wire.until(lambda: "nightly" in children(fx, CONTAINER)), containers(fx)
        (child,) = containers(fx)[CONTAINER]["children"]
        assert child["_meta"]["ahp-py/flow"]["source"] == "client"
        workspace = next(
            c for c in containers(fx).values() if c["id"] != CONTAINER and c.get("children")
        )
        nightly = next(c for c in workspace["children"] if c["name"] == "nightly")
        assert nightly["_meta"]["ahp-py/flow"]["shadowedBy"] == "client"

        turn = await run_turn(fx.wire, fx.chat, "t1", "/nightly")
        assert final_json(turn) == {"from": "client", "r": "rec: client hi"}
        turn = await run_turn(fx.wire, fx.chat, "t2", "/workspace:nightly")
        assert final_json(turn) == {"from": "workspace"}
        turn = await run_turn(fx.wire, fx.chat, "t3", "/client:evil")
        assert "evil" in text_of(turn)
        assert "```json" not in text_of(turn)
        assert not any(
            s.startswith("resourceRead") and s.endswith("evil.py") for s in fx.wire.served
        )
    finally:
        await fx.shut()


async def test_client_workflows_reach_workflow_call(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path, WITH_CLIENT)
    try:
        fx.wire.resources[f"{PLUGIN}/workflows/x.js"] = (
            "export const meta = {name:'x', description:'client x', phases:[]}\n"
            "return {from: 'client'}\n"
        )
        workflows = fx.workspace / ".claude" / "workflows"
        (workflows / "x.js").write_text(
            "export const meta = {name:'x', description:'ws x', phases:[]}\n"
            "return {from: 'workspace'}\n",
            encoding="utf-8",
        )
        nope = "try { await workflow('nope:x') } catch (e) { return e.message }"
        (workflows / "parent.js").write_text(
            "export const meta = {name:'parent', description:'calls x', phases:[]}\n"
            "return [await workflow('x'), await workflow('workspace:x'),\n"
            f"  await (async () => {{ {nope} }})()]\n",
            encoding="utf-8",
        )
        await publish(fx, [plugin_entry("n1")])
        assert await fx.wire.until(lambda: "x" in children(fx, CONTAINER)), containers(fx)
        turn = await run_turn(fx.wire, fx.chat, "t1", "/workspace:parent")
        assert final_json(turn) == [
            {"from": "client"},
            {"from": "workspace"},
            "unknown workflow source 'nope' in 'nope:x': use one of client, workspace, user, host",
        ]
    finally:
        await fx.shut()


async def test_client_workflows_off_by_default(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path, TOML)
    try:
        await publish(fx, [plugin_entry("n1")])
        turn = await run_turn(fx.wire, fx.chat, "t1", "/client:nightly")
        assert "```json" not in text_of(turn)
        assert not any(c.startswith("ahp-host-flow.client.") for c in containers(fx))
        # The host's own plugin expansion (host.py) probes every directory of the plugin
        # as a possible skill, so it reads `workflows/SKILL.md` regardless of this feature
        # (measured). What must not happen is this provider listing or reading the workflows.
        assert not any(
            "/workflows" in s and not s.endswith("/workflows/SKILL.md") for s in fx.wire.served
        ), fx.wire.served
    finally:
        await fx.shut()


async def test_client_republish_updates_customizations(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path, WITH_CLIENT)
    try:
        await publish(fx, [plugin_entry("n1")])
        assert await fx.wire.until(lambda: children(fx, CONTAINER) == ["nightly"]), containers(fx)
        fx.wire.resources[f"{PLUGIN}/workflows/second.js"] = SECOND
        await publish(fx, [plugin_entry("n2")])
        assert await fx.wire.until(lambda: children(fx, CONTAINER) == ["nightly", "second"]), (
            containers(fx)
        )
        await publish(fx, [])
        assert await fx.wire.until(lambda: CONTAINER not in containers(fx)), containers(fx)
    finally:
        await fx.shut()
