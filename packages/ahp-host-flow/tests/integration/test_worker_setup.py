"""A worker whose setup fails or is cancelled after its inner session exists is cleaned up.

The inner session is closed and the session's registries (`workers`, `worker_providers`)
keep no entry for the worker chat, whatever stops the setup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.provider import FlowSession
from tests import flow_fakes
from tests.flow_fakes import GatedProvider, RecordingSession
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import cancel_turn, notifications, start_turn, wait_turn, worker_chats

TOML = '[providers.gate]\nload = "tests.flow_fakes:GatedProvider"\n'
WORKFLOW = "meta = {'name': 'one', 'description': 'd'}\n\nasync def main(args):\n"
WORKFLOW += "    return await agent('hi')\n"


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    flow_fakes.reset_fakes()
    yield
    flow_fakes.reset_fakes()


def _flow(fx: FlowFixture) -> FlowSession:
    session = fx.host._sessions[SESSION].agent_session
    assert isinstance(session, FlowSession)
    return session


async def _worker_with_pending_overlay(tmp_path: Path) -> tuple[FlowFixture, str]:
    """Start the run and queue an overlay for its worker chat while the session is made."""
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "one.py").write_text(WORKFLOW, encoding="utf-8")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    await start_turn(fx.wire, fx.chat, "t1", "/one")
    assert await fx.wire.until(
        lambda: bool(GatedProvider.instances) and GatedProvider.instances[0].creating.is_set()
    )
    (worker,) = worker_chats(fx.host, SESSION)
    chat = str(worker["resource"])
    await fx.wire.request("subscribe", {"channel": chat})
    await fx.wire.dispatch(chat, {"type": "chat/configChanged", "config": {"effort": "high"}})
    assert await fx.wire.until(lambda: chat in _flow(fx)._chat_overlays)
    return fx, chat


def _clean(fx: FlowFixture) -> bool:
    flow = _flow(fx)
    return not flow._workers and not flow._worker_providers


async def test_worker_setup_a_failed_overlay_closes_the_inner_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(self: RecordingSession, chat: str, values: Any) -> None:
        raise RuntimeError("no overlay for you")

    monkeypatch.setattr(RecordingSession, "chat_config_changed", refuse)
    fx, _ = await _worker_with_pending_overlay(tmp_path)
    try:
        GatedProvider.instances[0].release.set()
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) is None
        assert any("no overlay for you" in n for n in notifications(turn)), notifications(turn)
        (session,) = GatedProvider.instances[0].sessions
        assert session.closed
        assert _clean(fx)
    finally:
        await fx.shut()


async def test_worker_setup_a_cancel_during_setup_closes_the_inner_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    async def hang(self: RecordingSession, chat: str, values: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RecordingSession, "chat_config_changed", hang)
    fx, _ = await _worker_with_pending_overlay(tmp_path)
    try:
        GatedProvider.instances[0].release.set()
        await asyncio.wait_for(entered.wait(), 10)
        await cancel_turn(fx.wire, fx.chat, "t1")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert turn["state"] == "cancelled"
        (session,) = GatedProvider.instances[0].sessions
        assert await fx.wire.until(lambda: session.closed)
        assert await fx.wire.until(lambda: _clean(fx))
    finally:
        await fx.shut()


async def test_worker_setup_a_failing_approver_closes_the_inner_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The turn's approval rules are built after the session is made; a failure there
    # (a working directory that is gone, say) must not leave the session open.
    from ahp_host_flow.dispatch import ProviderDispatcher

    def broken(self: Any, *args: Any) -> None:
        raise FileNotFoundError("the working directory is gone")

    monkeypatch.setattr(ProviderDispatcher, "_approver_for", broken)
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "one.py").write_text(WORKFLOW, encoding="utf-8")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/one")
        assert await fx.wire.until(lambda: bool(GatedProvider.instances))
        GatedProvider.instances[0].release.set()
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) is None
        (session,) = GatedProvider.instances[0].sessions
        assert session.closed
        assert _clean(fx)
    finally:
        await fx.shut()
