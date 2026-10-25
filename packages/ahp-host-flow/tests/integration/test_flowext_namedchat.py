"""`opts.x.chat` names a worker chat whose inner session is reused across calls."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ahp_host_flow import dispatch
from tests import flow_fakes
from tests.flow_fakes import RecordingProvider, RestartHoldProvider
from tests.flow_hosting import SESSION, boot_flow, final_json, restart_flow, store_for
from tests.hosting import found, notifications, run_turn, start_turn, state, wait_turn, worker_chats
from tests.integration.test_flow_durability import wait_resumed

TOML = (
    '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n\n[routing]\ndefault = "rec"\n'
)
TOML_HOLD = (
    '[providers.hold]\nload = "tests.flow_fakes:RestartHoldProvider"\n\n'
    '[routing]\ndefault = "hold"\n'
)
FOLLOW_JS = (
    "const a = await agent('one', {x: {chat: 'pair'}}); "
    "const b = await agent('two', {x: {chat: 'pair'}}); "
    "const c = await agent('solo'); "
    "return [a, b, c]"
)
FOLLOW_PY = (
    "meta = {'name': 'pair', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await agent('one', x={'chat': 'pair'})\n"
    "    b = await agent('two', x={'chat': 'pair'})\n"
    "    c = await agent('solo')\n"
    "    return [a, b, c]\n"
)


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    flow_fakes.reset_fakes()
    yield
    flow_fakes.reset_fakes()


def js(body: str) -> str:
    return "```js\n" + body + "\n```"


async def _follow_up(tmp_path: Path, node: str, text: str) -> None:
    fx = await boot_flow(tmp_path, node, TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", text)
        assert final_json(turn) == ["rec: one", "rec: two", "rec: solo"]
        (rec,) = RecordingProvider.instances
        assert len(rec.sessions) == 2
        # RecordingSession is an eq dataclass (unhashable): compare by position.
        (named,) = (s for s in rec.sessions if s.messages and s.messages[0].text == "one")
        (solo,) = (s for s in rec.sessions if s is not named)
        assert [m.text for m in named.messages] == ["one", "two"]
        assert named.closed
        assert [m.text for m in solo.messages] == ["solo"]
        assert solo.closed
        chats = worker_chats(fx.host, SESSION)
        assert len(chats) == 2
        (pair,) = (c for c in chats if c.get("title") == "one")
        assert len(state(fx.host, pair["resource"])["turns"]) == 2
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_namedchat_follow_up_js(tmp_path: Path, node_path: str) -> None:
    await _follow_up(tmp_path, node_path, js(FOLLOW_JS))


async def test_flowext_namedchat_follow_up_py(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "pair.py").write_text(FOLLOW_PY, encoding="utf-8")
    await _follow_up(tmp_path, str(tmp_path / "no-node"), "/pair")


@pytest.mark.node
async def test_flowext_namedchat_serialized(tmp_path: Path, node_path: str) -> None:
    body = (
        "return await parallel(["
        "() => agent('hold-1', {x: {chat: 's'}}), "
        "() => agent('two', {x: {chat: 's'}})])"
    )
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        await start_turn(fx.wire, fx.chat, "t1", js(body))

        def both_started() -> bool:
            calls = found(
                state(fx.host, fx.chat).get("activeTurn"),
                lambda m: m.get("toolName") == "agent",
            )
            ids = {c.get("toolCallId") for c in calls}
            if len(ids) < 2 or not RecordingProvider.instances:
                return False
            sessions = RecordingProvider.instances[0].sessions
            return bool(sessions) and [m.text for m in sessions[0].messages] == ["hold-1"]

        assert await fx.wire.until(both_started), state(fx.host, fx.chat)
        (rec,) = RecordingProvider.instances
        assert len(rec.sessions) == 1
        assert [m.text for m in rec.sessions[0].messages] == ["hold-1"]
        rec.sessions[0].released.set()
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == ["rec: hold-1", "rec: two"]
        assert [m.text for m in rec.sessions[0].messages] == ["hold-1", "two"]
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_namedchat_refusals(tmp_path: Path, node_path: str) -> None:
    body = (
        "const a = await agent('a', {x: {chat: ''}}); "
        "const b = await agent('b', {isolation: 'worktree', x: {chat: 'w'}}); "
        "return [a, b]"
    )
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", js(body))
        assert final_json(turn) == [None, None]
        notes = notifications(turn)
        assert any("opts.x.chat must be a non-empty string" in n for n in notes)
        assert any("opts.x.chat cannot be combined with isolation: 'worktree'" in n for n in notes)
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_namedchat_resume_recreates(tmp_path: Path, node_path: str) -> None:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "named.js").write_text(
        "const a = await agent('one', {x: {chat: 'p'}})\n"
        "const b = await agent('hold two', {x: {chat: 'p'}})\n"
        "return {a, b}\n",
        encoding="utf-8",
    )
    fx = await boot_flow(tmp_path, node_path, TOML_HOLD, store=store_for(tmp_path))
    fx2 = None
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/named")
        assert await fx.wire.until(lambda: "hold two" in RestartHoldProvider.prompts)
        assert RestartHoldProvider.sessions_created == 1
        assert fx.chat in await fx.host.drain(0.2)
        await fx.shut()
        RestartHoldProvider.holding = False
        fx2 = await restart_flow(tmp_path, node_path, TOML_HOLD)
        turn = await wait_resumed(fx2)
        assert final_json(turn) == {"a": "rec: one", "b": "rec: hold two"}
        assert RestartHoldProvider.prompts == ["one", "hold two", "hold two"]
        assert RestartHoldProvider.sessions_created == 2
    finally:
        if fx2 is not None:
            await fx2.shut()


CONFIG_JS = (
    "const a = await agent('one', {effort: 'low', model: 'm1', x: {chat: 'pair'}}); "
    "const b = await agent('two', {effort: 'high', model: 'm2', x: {chat: 'pair'}}); "
    "const c = await agent('three', {effort: 'low', model: 'm1', x: {chat: 'pair'}}); "
    "return [a, b, c]"
)
CONFIG_PY = (
    "meta = {'name': 'cfg', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await agent('one', effort='low', model='m1', x={'chat': 'pair'})\n"
    "    b = await agent('two', effort='high', model='m2', x={'chat': 'pair'})\n"
    "    c = await agent('three', effort='low', model='m1', x={'chat': 'pair'})\n"
    "    return [a, b, c]\n"
)


async def _keeps_first_config(tmp_path: Path, node: str, text: str) -> None:
    # A named chat keeps its first call's config (effort, approval mode): the inner session
    # is created once. The model goes with each message. A follow-up asking for another
    # config says so in a log line instead of being silently ignored.
    fx = await boot_flow(tmp_path, node, TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", text)
        assert final_json(turn) == ["rec: one", "rec: two", "rec: three"]
        (rec,) = RecordingProvider.instances
        (session,) = rec.sessions
        assert dict(session.context.config or {}).get("effort") == "low"
        models = [m.model.id if m.model else None for m in session.messages]
        assert models == ["m1", "m2", "m1"]
        notes = [n for n in notifications(turn) if "keeps its first call's config" in n]
        assert notes == [
            "agent() #1: opts.x.chat 'pair' keeps its first call's config "
            '{"effort": "low"}; {"effort": "high"} ignored'
        ]
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_namedchat_keeps_first_config_js(tmp_path: Path, node_path: str) -> None:
    await _keeps_first_config(tmp_path, node_path, js(CONFIG_JS))


async def test_flowext_namedchat_keeps_first_config_py(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "cfg.py").write_text(CONFIG_PY, encoding="utf-8")
    await _keeps_first_config(tmp_path, str(tmp_path / "no-node"), "/cfg")


TIMEOUT_PY = (
    "meta = {{'name': 'late', 'description': 'd'}}\n"
    "\n"
    "async def main(args):\n"
    "{first}"
    "    a = await agent('hold-1', x={{'chat': 's', 'timeout': 0.3}})\n"
    "    b = await agent('two', x={{'chat': 's'}})\n"
    "    return [a, b]\n"
)


@pytest.mark.parametrize("first", ["", "    await agent('one', x={'chat': 's'})\n"])
async def test_flowext_namedchat_usable_after_a_timed_out_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    # The timed-out turn's inner session ignores the cancel: the call ends only once the
    # grace runs out and its turn task is cancelled, and the next call on the chat must wait
    # for that turn to end rather than find it still running.
    monkeypatch.setattr(dispatch, "ABORT_GRACE", 0.5)
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "late.py").write_text(TIMEOUT_PY.format(first=first), encoding="utf-8")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/late")
        assert final_json(turn) == [None, "rec: two"], notifications(turn)
    finally:
        await fx.shut()
