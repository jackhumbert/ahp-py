"""A run's Approvals change reaches its running workers (plan §8 M15).

Said on the run's main chat: a chat's config does not inherit from its session (upstream
`c301d494`), so a session-level change reaches none of them.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests import flow_fakes
from tests.flow_fakes import ModalProvider
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import start_turn, state, wait_turn
from tests.integration.test_flowext_hostgates import py

TOML = '[providers.modal]\nload = "tests.flow_fakes:ModalProvider"\n'
WORKFLOW = (
    "meta = {'name': 'modal', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('go', label='modal')\n"
)


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    flow_fakes.reset_fakes()
    yield
    flow_fakes.reset_fakes()


def _worker_entries(fx: FlowFixture) -> list[Any]:
    needed = state(fx.host, SESSION).get("inputNeeded") or []
    return [e for e in needed if isinstance(e, dict) and e.get("chat") != fx.chat]


async def _running(fx: FlowFixture, tmp_path: Path) -> dict[str, Any]:
    """Start the workflow and wait for the worker's first confirmation."""
    await start_turn(fx.wire, fx.chat, "t1", py(tmp_path, "modal", WORKFLOW))
    assert await fx.wire.until(lambda: bool(_worker_entries(fx))), state(fx.host, SESSION)
    entry: dict[str, Any] = _worker_entries(fx)[0]
    await fx.wire.request("subscribe", {"channel": str(entry["chat"])})
    return entry


async def test_bulkapprove_config_change_approves_a_running_workers_next_prompt(
    tmp_path: Path,
) -> None:
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        entry = await _running(fx, tmp_path)
        await fx.wire.dispatch(
            fx.chat, {"type": "chat/configChanged", "config": {"permissionMode": "auto"}}
        )
        session = ModalProvider.instances[0].sessions[0]
        assert await fx.wire.until(lambda: session.mode == "auto")
        await fx.wire.dispatch(
            str(entry["chat"]),
            {
                "type": "chat/toolCallConfirmed",
                "turnId": entry["turnId"],
                "toolCallId": entry["toolCall"]["toolCallId"],
                "approved": True,
                "confirmed": "user-action",
            },
        )
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "asked=1 mode=auto"
    finally:
        await fx.shut()


async def test_bulkapprove_effort_is_not_fanned_out(tmp_path: Path) -> None:
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        await _running(fx, tmp_path)
        await fx.wire.dispatch(
            fx.chat, {"type": "chat/configChanged", "config": {"effort": "high"}}
        )
        await fx.wire.dispatch(
            fx.chat, {"type": "chat/configChanged", "config": {"permissionMode": "auto"}}
        )
        session = ModalProvider.instances[0].sessions[0]
        assert await fx.wire.until(lambda: session.mode == "auto")
        assert session.overlays == [{"permissionMode": "auto"}]
    finally:
        await fx.shut()


async def test_bulkapprove_a_session_change_leaves_the_running_worker_asking(
    tmp_path: Path,
) -> None:
    """Security-relevant: the session's loosened Approvals does not reach a run whose main
    chat has its own values."""
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        await _running(fx, tmp_path)
        await fx.wire.dispatch(
            SESSION, {"type": "session/configChanged", "config": {"permissionMode": "auto"}}
        )
        assert await fx.wire.until(
            lambda: state(fx.host, SESSION)["config"]["values"].get("permissionMode") == "auto"
        )
        session = ModalProvider.instances[0].sessions[0]
        assert session.mode != "auto"
        assert session.overlays == []
    finally:
        await fx.shut()
