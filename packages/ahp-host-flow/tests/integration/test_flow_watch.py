"""Watched local workflow dirs (plan §4.3): a source change republishes with no message sent."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.provider import FlowProvider
from tests.flow_fakes import reset_fakes
from tests.flow_hosting import SESSION, FlowFixture, boot_flow
from tests.hosting import state

TOML = """
[providers.rec]
load = "tests.flow_fakes:RecordingProvider"

[routing]
default = "rec"

[workflows]
watch_interval = {interval}
"""

WAIT = 10.0


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def described(description: str) -> str:
    return f"export const meta = {{name:'new', description:'{description}', phases:[]}}\nreturn 1\n"


def customizations(fx: FlowFixture) -> list[Any]:
    return list(state(fx.host, SESSION).get("customizations", []))


def children(fx: FlowFixture) -> dict[str, Any]:
    """The workspace container's children by name."""
    for container in customizations(fx):
        if container.get("id") == "ahp-host-flow.workspace.0":
            return {str(c["name"]): c for c in container.get("children", [])}
    return {}


def description_of(fx: FlowFixture, name: str) -> Any:
    return children(fx).get(name, {}).get("description")


def no_message_sent(fx: FlowFixture) -> bool:
    return bool(state(fx.host, fx.chat)["turns"] == [])


async def test_flow_watch_add_edit_delete_publishes(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, TOML.format(interval=0.05))
    path = fx.workspace / ".claude" / "workflows" / "new.js"
    try:
        path.write_text(described("one"), encoding="utf-8")
        assert await fx.wire.until(lambda: description_of(fx, "new") == "one", timeout=WAIT)
        assert no_message_sent(fx)
        # The size changes, so the edit is seen whatever the mtime resolution.
        path.write_text(described("three"), encoding="utf-8")
        assert await fx.wire.until(lambda: description_of(fx, "new") == "three", timeout=WAIT)
        assert no_message_sent(fx)
        path.unlink()
        assert await fx.wire.until(lambda: "new" not in children(fx), timeout=WAIT)
        assert no_message_sent(fx)
    finally:
        await fx.shut()


async def test_flow_watch_new_workspace_dir_is_picked_up(tmp_path: Path) -> None:
    fx = await boot_flow(tmp_path, "no-node", TOML.format(interval=0.05))
    try:
        late = fx.workspace / ".claude" / "workflows"
        shutil.rmtree(fx.workspace / ".claude")
        assert await fx.wire.until(lambda: not children(fx), timeout=WAIT)
        late.mkdir(parents=True)
        (late / "late.js").write_text(described("x"), encoding="utf-8")
        assert await fx.wire.until(lambda: "late" in children(fx), timeout=WAIT)
        assert no_message_sent(fx)
    finally:
        await fx.shut()


async def test_flow_watch_interval_zero_publishes_nothing(tmp_path: Path) -> None:
    fx = await boot_flow(tmp_path, "no-node", TOML.format(interval=0))
    try:
        provider = fx.host.provider
        assert isinstance(provider, FlowProvider)
        sessions = list(provider._sessions)
        assert sessions
        assert all(s._watcher is None for s in sessions)
        before = customizations(fx)
        (fx.workspace / ".claude" / "workflows" / "w.js").write_text(
            described("x"), encoding="utf-8"
        )
        # 20x the 0.05 s interval the positive tests use.
        await asyncio.sleep(1.0)
        assert customizations(fx) == before
        assert no_message_sent(fx)
    finally:
        await fx.shut()
