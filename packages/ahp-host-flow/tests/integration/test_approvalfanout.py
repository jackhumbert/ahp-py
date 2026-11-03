"""An Approvals change reaches the inner sessions it applies to (plan §8 M15).

A named chat's inner session is created once, so a mode chosen between its calls must be
forwarded to it; and a change to the run's main chat applies to that run's live workers at
once. A chat's config does not inherit from its session (upstream `c301d494`), so the
session's own Approvals change reaches none of them: it is the seed for chats created later.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import flow_fakes
from tests.flow_fakes import RecordingProvider, RecordingSession
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import start_turn, state, wait_turn, worker_chats

TOML = '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n'
AUTO = {"permissionMode": "auto"}


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    flow_fakes.reset_fakes()
    yield
    flow_fakes.reset_fakes()


async def _boot(tmp_path: Path, name: str, body: str) -> FlowFixture:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    source = f"meta = {{'name': {name!r}, 'description': 'd'}}\n\nasync def main(args):\n{body}"
    (user / f"{name}.py").write_text(source, encoding="utf-8")
    return await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)


def _sessions() -> list[RecordingSession]:
    return [s for p in RecordingProvider.instances for s in p.sessions]


def _session_of(text: str) -> RecordingSession:
    (found,) = (s for s in _sessions() if s.messages and s.messages[0].text == text)
    return found


async def test_approvalfanout_reaches_a_named_chat_between_calls(tmp_path: Path) -> None:
    fx = await _boot(
        tmp_path,
        "named",
        "    a = await agent('one', x={'chat': 'p'})\n"
        "    h = await agent('hold h')\n"
        "    b = await agent('two', x={'chat': 'p'})\n"
        "    return [a, h, b]\n",
    )
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/named")
        assert await fx.wire.until(lambda: any(s.messages[:1] for s in _sessions()[1:]))
        await fx.wire.dispatch(fx.chat, {"type": "chat/configChanged", "config": AUTO})
        held = _session_of("hold h")
        # The running worker takes the change at once (a fan-out of the main chat's).
        assert await fx.wire.until(held.overlay_seen.is_set)
        held.released.set()
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        named = _session_of("one")
        (pair,) = (c for c in worker_chats(fx.host, SESSION) if c.get("title") == "one")
        assert named.overlays == [(pair["resource"], AUTO)]
        assert final_json(turn)[2] == 'rec: two overlay={"permissionMode": "auto"}'
    finally:
        await fx.shut()


async def test_approvalfanout_main_chat_overlay_reaches_the_runs_workers(tmp_path: Path) -> None:
    fx = await _boot(tmp_path, "live", "    return await agent('wait-overlay')\n")
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/live")
        assert await fx.wire.until(lambda: bool(_sessions() and _sessions()[0].messages))
        await fx.wire.dispatch(fx.chat, {"type": "chat/configChanged", "config": AUTO})
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == 'rec: wait-overlay overlay={"permissionMode": "auto"}'
        (worker,) = worker_chats(fx.host, SESSION)
        assert _sessions()[0].overlays == [(worker["resource"], AUTO)]
    finally:
        await fx.shut()


async def test_approvalfanout_a_session_change_does_not_reach_a_chat_with_values(
    tmp_path: Path,
) -> None:
    """Security-relevant: loosening the session's Approvals must not loosen a run whose
    main chat has its own values (every chat of a configured session does)."""
    fx = await _boot(tmp_path, "held", "    return await agent('wait-overlay')\n")
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/held")
        assert await fx.wire.until(lambda: bool(_sessions() and _sessions()[0].messages))
        await fx.wire.dispatch(SESSION, {"type": "session/configChanged", "config": AUTO})
        assert await fx.wire.until(
            lambda: state(fx.host, SESSION)["config"]["values"].get("permissionMode") == "auto"
        )
        assert _sessions()[0].overlays == [], "the session's change reached a running worker"
        # The run's own chat does: that is where "approve all for this run" is said.
        await fx.wire.dispatch(fx.chat, {"type": "chat/configChanged", "config": AUTO})
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == 'rec: wait-overlay overlay={"permissionMode": "auto"}'
    finally:
        await fx.shut()
