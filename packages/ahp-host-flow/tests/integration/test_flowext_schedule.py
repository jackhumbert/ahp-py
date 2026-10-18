"""meta.x.schedule: offered on the workflow's customization, applied by a client (M10)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ahp_host.core.automations import InMemoryAutomationStore
from ahp_protocol.ext import flow

from tests.flow_fakes import reset_fakes
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import found, state
from tests.integration.test_flow_automations import (
    AUTOMATION,
    definition,
    entries,
    run_to_end,
    save,
)

pytestmark = pytest.mark.node

TOML = """
[providers.rec]
load = "tests.flow_fakes:RecordingProvider"

[routing]
default = "rec"
"""

NIGHTLY = (
    "export const meta = {name: 'nightly', description: 'n', x: {schedule: '0 6 * * 1-5'}}\n"
    "return 1\n"
)
BROKEN = (
    "export const meta = {name: 'broken', description: 'b', x: {schedule: 'every day'}}\nreturn 2\n"
)
NIGHTLY_PY = (
    "meta = {'name': 'nightly_py', 'description': 'n', 'x': {'schedule': "
    "{'expression': '30 7 * * *', 'timeZone': 'Europe/Berlin'}}}\n\n"
    "async def main(args):\n"
    "    return 1\n"
)


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def children(fx: FlowFixture) -> list[dict[str, Any]]:
    for container in state(fx.host, SESSION).get("customizations", []):
        if container.get("id") == "ahp-host-flow.user":
            return list(container.get("children", []))
    return []


async def boot(tmp_path: Path, node_path: str) -> tuple[FlowFixture, dict[str, dict[str, Any]]]:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "nightly.js").write_text(NIGHTLY, encoding="utf-8")
    (user / "broken.js").write_text(BROKEN, encoding="utf-8")
    (user / "nightly_py.py").write_text(NIGHTLY_PY, encoding="utf-8")
    fx = await boot_flow(tmp_path, node_path, TOML, automations=InMemoryAutomationStore())
    assert await fx.wire.until(lambda: len(children(fx)) == 3), children(fx)
    return fx, {str(c["name"]): c for c in children(fx)}


async def test_flowext_schedule_offered(tmp_path: Path, node_path: str) -> None:
    fx, by_name = await boot(tmp_path, node_path)
    try:
        meta = by_name["nightly"]["_meta"][flow.FLOW_META_KEY]
        assert meta["schedule"] == {"expression": "0 6 * * 1-5", "timeZone": "UTC"}
        meta = by_name["nightly_py"]["_meta"][flow.FLOW_META_KEY]
        assert meta["schedule"] == {"expression": "30 7 * * *", "timeZone": "Europe/Berlin"}
        meta = by_name["broken"]["_meta"][flow.FLOW_META_KEY]
        assert "schedule" not in meta
        assert meta["warnings"][0].startswith("meta.x.schedule 'every day' is not a valid schedule")
    finally:
        await fx.shut()


async def test_flowext_schedule_applied(tmp_path: Path, node_path: str) -> None:
    fx, by_name = await boot(tmp_path, node_path)
    try:
        saved = flow.with_offered_schedule(definition(fx, "/nightly"), by_name["nightly"])
        await save(fx, saved)
        (entry,) = found(entries(fx), lambda m: m.get("id") == "ahp-py/flow-schedule")
        assert entry["kind"] == "schedule"
        assert entry["schedule"] == {"expression": "0 6 * * 1-5", "timeZone": "UTC"}
        assert any(e["resource"] == AUTOMATION for e in entries(fx))
        assert final_json(await run_to_end(fx)) == 1
    finally:
        await fx.shut()
