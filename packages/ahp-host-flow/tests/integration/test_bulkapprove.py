"""A session Approvals change reaches the running workers (plan §8 M15)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
            SESSION, {"type": "session/configChanged", "config": {"permissionMode": "auto"}}
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
            SESSION, {"type": "session/configChanged", "config": {"effort": "high"}}
        )
        await fx.wire.dispatch(
            SESSION, {"type": "session/configChanged", "config": {"permissionMode": "auto"}}
        )
        session = ModalProvider.instances[0].sessions[0]
        assert await fx.wire.until(lambda: session.mode == "auto")
        assert session.overlays == [{"permissionMode": "auto"}]
    finally:
        await fx.shut()
