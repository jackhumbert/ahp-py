"""A run cut off by a host restart is resumed from its journal (plan §4.8), in JS and Python.

The host fails a turn the restart cut off (`host.restart`, resumable) and resumes
it through `chat/turnResume`: by itself, once, after a drain-restart; on a
client's request after a crash. `FlowSession.resume_turn` relaunches the run
from its journal.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.ext import flow

from ahp_host_flow.engine import load_journal
from ahp_host_flow.provider import NOTHING_TO_RESUME
from tests.flow_fakes import RestartHoldProvider, reset_fakes
from tests.flow_hosting import (
    SESSION,
    FlowFixture,
    boot_flow,
    descriptors,
    final_json,
    restart_flow,
    store_for,
)
from tests.hosting import found, notifications, start_turn, state, worker_chats

TOML = (
    '[providers.hold]\nload = "tests.flow_fakes:RestartHoldProvider"\n'
    '\n[routing]\ndefault = "hold"\n'
)
DURABLE_JS = (
    "const a = await agent('one')\n"
    "const b = await agent('two')\n"
    "const c = await agent('hold three')\n"
    "return {a, b, c}\n"
)
DURABLE_PY = (
    "meta = {'name': 'durable_py', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await agent('one')\n"
    "    b = await agent('two')\n"
    "    c = await agent('hold three')\n"
    "    return {'a': a, 'b': b, 'c': c}\n"
)
PHASED_JS = (
    "phase('Gather')\n"
    "const a = await agent('one')\n"
    "phase('Work')\n"
    "const b = await agent('two')\n"
    "const c = await agent('hold three')\n"
    "return {a, b, c}\n"
)
PHASED_PY = (
    "meta = {'name': 'phased_py', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    phase('Gather')\n"
    "    a = await agent('one')\n"
    "    phase('Work')\n"
    "    b = await agent('two')\n"
    "    c = await agent('hold three')\n"
    "    return {'a': a, 'b': b, 'c': c}\n"
)
EXPECTED = {"a": "rec: one", "b": "rec: two", "c": "rec: hold three"}


def write_workflows(base: Path) -> None:
    user = base / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "durable.js").write_text(DURABLE_JS, encoding="utf-8")
    (user / "durable_py.py").write_text(DURABLE_PY, encoding="utf-8")
    (user / "phased.js").write_text(PHASED_JS, encoding="utf-8")
    (user / "phased_py.py").write_text(PHASED_PY, encoding="utf-8")


def step_calls(turn: Mapping[str, Any], ordinal: int) -> list[Mapping[str, Any]]:
    """The main chat's tool calls for script step *ordinal*, in transcript order."""
    calls = [
        part["toolCall"] for part in turn.get("responseParts", []) if part.get("kind") == "toolCall"
    ]
    return [
        call
        for call in calls
        if ((flow.descriptor_of(call.get("_meta")) or {}).get("stepStarted") or {}).get("ordinal")
        == ordinal
    ]


def step_finished(call: Mapping[str, Any]) -> Mapping[str, Any]:
    finished = (flow.descriptor_of(call.get("_meta")) or {}).get("stepFinished")
    assert isinstance(finished, Mapping), call
    return finished


def error_types(value: Any) -> list[str]:
    return [str(e["error"]["errorType"]) for e in found(value, lambda m: m.get("kind") == "error")]


def assert_cut_off_step_closed_before_redispatch(
    turn: Mapping[str, Any], *, redispatches: int = 1
) -> None:
    """The in-flight step was closed when the restart failed the turn, then ran again live."""
    calls = step_calls(turn, 2)
    assert len(calls) == redispatches + 1, calls
    *closed, live = calls
    for call in closed:
        assert call["status"] == "cancelled", call
    assert step_finished(live)["status"] == "ok"
    ids = [str(c["toolCallId"]) for c in calls]
    assert len(set(ids)) == len(ids), "re-dispatched calls must get ids of their own"
    assert ids[-1].startswith(f"agent-r{redispatches}-"), ids


async def wait_resumed(fx: FlowFixture, turn_id: str = "t1") -> dict[str, Any]:
    """Wait for *turn_id* to be the main chat's latest turn, ended and not by the restart."""

    def done() -> dict[str, Any] | None:
        chat_state = state(fx.host, fx.chat)
        turns = chat_state.get("turns", [])
        latest = turns[-1] if turns else None
        if chat_state.get("activeTurn") is not None or not isinstance(latest, Mapping):
            return None
        if latest.get("id") != turn_id or latest.get("state") == "error":
            return None
        return dict(latest)

    assert await fx.wire.until(lambda: done() is not None, timeout=20.0), state(fx.host, fx.chat)
    turn = done()
    assert turn is not None
    return turn


async def cut_off(tmp_path: Path, node: str, command: str, *, drain: bool) -> None:
    """Run *command* until its third call is in flight, then stop the host.

    With *drain* the stop is a deliberate restart whose deadline passed; without,
    it stands for a crash (the turn active in storage, nothing saying why).
    """
    reset_fakes()
    write_workflows(tmp_path)
    fx = await boot_flow(tmp_path, node, TOML, store=store_for(tmp_path))
    await start_turn(fx.wire, fx.chat, "t1", command)
    assert await fx.wire.until(lambda: "hold three" in RestartHoldProvider.prompts)
    if drain:
        assert fx.chat in await fx.host.drain(0.2)
    await fx.shut()


async def resume_by_hand(fx: FlowFixture) -> None:
    """What a client does with a turn a crash failed: `chat/turnResume`."""
    await fx.wire.dispatch(fx.chat, {"type": "chat/turnResume", "turnId": "t1"})


def assert_one_restart_error(turn: Mapping[str, Any], *, resumes: int = 1) -> None:
    """The turn carries the restart's error part(s) from before it was resumed, and no other."""
    assert turn["state"] == "complete"
    assert error_types(turn) == ["host.restart"] * resumes


async def _check(tmp_path: Path, node: str, command: str, *, drain: bool) -> None:
    await cut_off(tmp_path, node, command, drain=drain)
    RestartHoldProvider.holding = False
    fx2 = await restart_flow(tmp_path, node, TOML)
    try:
        if not drain:
            # A crash resumes nothing by itself: the turn waits, failed and resumable.
            await asyncio.sleep(0.3)
            assert RestartHoldProvider.prompts == ["one", "two", "hold three"]
            failed = state(fx2.host, fx2.chat)["turns"][-1]
            assert (failed["id"], failed["state"]) == ("t1", "error")
            assert failed["responseParts"][-1]["error"]["errorType"] == "host.restart"
            assert failed["responseParts"][-1]["resumable"] is True
            await resume_by_hand(fx2)
        turn = await wait_resumed(fx2)
        assert final_json(turn) == EXPECTED
        assert_one_restart_error(turn)
        # The finished calls were not re-dispatched; the in-flight one exactly once more.
        assert RestartHoldProvider.prompts == ["one", "two", "hold three", "hold three"]
        RestartHoldProvider.prompts.clear()
        (tmp_path / "plain").mkdir()
        write_workflows(tmp_path / "plain")
        plain = await boot_flow(tmp_path / "plain", node, TOML)
        try:
            await start_turn(plain.wire, plain.chat, "t1", command)
            plain_turn = await wait_resumed(plain)
            assert final_json(plain_turn) == final_json(turn)
        finally:
            await plain.shut()

        lines = notifications(turn)
        assert any(
            line.startswith("resume: agent() #2 started on hold (")
            and line.endswith(") but was cut off by a host restart; dispatching it again")
            for line in lines
        ), lines
        assert any(
            re.fullmatch(r"Run \S+ resumed after a host restart as run \S+", line) for line in lines
        ), lines

        assert len([d for d in descriptors(turn) if "runStarted" in d]) == 1
        journals = [
            load_journal(tmp_path / "state", d.name)
            for d in sorted((tmp_path / "state" / "runs").iterdir())
        ]
        (original,) = [j for j in journals if j.header.resumed_from is None]
        view = flow.run_view(state(fx2.host, fx2.chat), turn_id="t1")
        assert view.status == "ok"
        assert view.run_id == original.header.run_id
        assert [s.status for s in view.steps] == ["ok", "ok", "ok"]
        assert_cut_off_step_closed_before_redispatch(turn)

        # The worker chat the cut-off step ran in: failed by the restart, and
        # never resumed (its call was dispatched again by the run instead).
        cut = []
        for chat in worker_chats(fx2.host, SESSION):
            last = (state(fx2.host, str(chat["resource"])).get("turns") or [{}])[-1]
            if "host.restart" in error_types(last):
                cut.append(last)
        assert len(cut) == 1
        assert cut[0]["state"] == "error"
        assert "rec: hold three" not in str(cut[0])
        # A worker chat is known as one after the restart too (`provider_chats` is restored
        # from its `tool` origin), so its turn is not offered for resuming: the run's own
        # resume dispatches the call again instead.
        assert error_types(cut[0]) == ["host.restart"]
        assert "'resumable': True" not in str(cut[0])
        assert NOTHING_TO_RESUME not in str(cut[0])
    finally:
        await fx2.shut()


def phase_counts(turn: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    """Per phase title, how many `phaseStarted` and `phaseFinished` notices the turn holds."""
    counts: dict[str, list[int]] = {}
    for d in descriptors(turn):
        for index, name in enumerate(("phaseStarted", "phaseFinished")):
            body = d.get(name)
            if isinstance(body, Mapping):
                counts.setdefault(str(body["title"]), [0, 0])[index] += 1
    return {title: (n[0], n[1]) for title, n in counts.items()}


async def _phased(tmp_path: Path, node: str, command: str) -> None:
    await cut_off(tmp_path, node, command, drain=True)
    RestartHoldProvider.holding = False
    fx2 = await restart_flow(tmp_path, node, TOML)
    try:
        turn = await wait_resumed(fx2)
        assert final_json(turn) == EXPECTED
        assert_cut_off_step_closed_before_redispatch(turn)
        # One phase per title across the restart: the continued run replays the
        # script's `phase()` calls, which must not open them a second time.
        assert phase_counts(turn) == {"Gather": (1, 1), "Work": (1, 1)}, phase_counts(turn)
        view = flow.run_view(state(fx2.host, fx2.chat), turn_id="t1")
        assert [p.title for p in view.phases] == ["Gather", "Work"]
        assert view.status == "ok"
    finally:
        await fx2.shut()


async def _chained(tmp_path: Path, node: str, command: str) -> None:
    """Restart twice: the resumed run is itself cut off, then resumed again."""
    await cut_off(tmp_path, node, command, drain=True)
    # Still holding: the resumed run dispatches `hold three` and is cut off again.
    fx2 = await restart_flow(tmp_path, node, TOML)
    assert await fx2.wire.until(lambda: RestartHoldProvider.prompts.count("hold three") == 2)
    assert fx2.chat in await fx2.host.drain(0.2)
    await fx2.shut()
    RestartHoldProvider.holding = False
    fx3 = await restart_flow(tmp_path, node, TOML)
    try:
        turn = await wait_resumed(fx3)
        assert final_json(turn) == EXPECTED
        assert RestartHoldProvider.prompts == ["one", "two", "hold three"] + ["hold three"] * 2
        assert_cut_off_step_closed_before_redispatch(turn, redispatches=2)
        assert len([d for d in descriptors(turn) if "runStarted" in d]) == 1
        assert_one_restart_error(turn, resumes=2)
        view = flow.run_view(state(fx3.host, fx3.chat), turn_id="t1")
        assert view.status == "ok"
        assert [s.status for s in view.steps] == ["ok", "ok", "ok"]
    finally:
        await fx3.shut()


@pytest.mark.node
async def test_flow_restart_js_run_resumes_after_a_drain(tmp_path: Path, node_path: str) -> None:
    await _check(tmp_path, node_path, "/durable", drain=True)


async def test_flow_restart_py_run_resumes_after_a_drain(tmp_path: Path) -> None:
    await _check(tmp_path, str(tmp_path / "no-node"), "/durable_py", drain=True)


@pytest.mark.node
async def test_flow_restart_js_run_lost_to_a_crash_resumes_on_request(
    tmp_path: Path, node_path: str
) -> None:
    await _check(tmp_path, node_path, "/durable", drain=False)


async def test_flow_restart_py_run_lost_to_a_crash_resumes_on_request(tmp_path: Path) -> None:
    await _check(tmp_path, str(tmp_path / "no-node"), "/durable_py", drain=False)


@pytest.mark.node
async def test_flow_restart_js_phases_stay_one_per_title(tmp_path: Path, node_path: str) -> None:
    await _phased(tmp_path, node_path, "/phased")


async def test_flow_restart_py_phases_stay_one_per_title(tmp_path: Path) -> None:
    await _phased(tmp_path, str(tmp_path / "no-node"), "/phased_py")


@pytest.mark.node
async def test_flow_restart_js_run_resumes_twice(tmp_path: Path, node_path: str) -> None:
    await _chained(tmp_path, node_path, "/durable")


async def test_flow_restart_py_run_resumes_twice(tmp_path: Path) -> None:
    await _chained(tmp_path, str(tmp_path / "no-node"), "/durable_py")
