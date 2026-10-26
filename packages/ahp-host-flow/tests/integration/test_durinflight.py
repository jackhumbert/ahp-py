"""A cut-off unnamed call on a provider that resumes turns is resumed in place (M17.3b).

A call the restart cut off continues in its worker chat through `resume_turn` when the run
recorded the inner session's state at the turn's start; a schema call is dispatched again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.provider import FlowSession
from tests import flow_fakes
from tests.flow_fakes import RESUMED_IN_PLACE, ResumableHoldProvider, reset_fakes
from tests.flow_hosting import SESSION, boot_flow, final_json, restart_flow, store_for
from tests.hosting import notifications, start_turn, worker_chats
from tests.integration.test_flow_durability import resume_by_hand, wait_resumed

NO_NODE = "no-node"
PLAIN = (
    "meta = {'name': 'durinflight', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('hold one')\n"
)
SCHEMA = (
    "meta = {'name': 'durschema', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('hold one', schema={'type': 'object'})\n"
)
TOML = (
    '[providers.rh]\nload = "tests.flow_fakes:ResumableHoldProvider"\n\n[routing]\ndefault = "rh"\n'
)


async def _cut_off(
    tmp_path: Path, node: str, name: str, source: str, *, drain: bool, reset: bool = True
) -> str:
    """Run the workflow until `hold one` is in flight, stop the host; the worker chat's URI."""
    if reset:
        reset_fakes()
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / f"{name}.py").write_text(source, encoding="utf-8")
    fx = await boot_flow(tmp_path, node, TOML, store=store_for(tmp_path))
    await start_turn(fx.wire, fx.chat, "t1", f"/{name}")
    # A schema call's prompt carries the schema instructions after `hold one`.
    assert await fx.wire.until(
        lambda: any(p.startswith("hold one") for p in ResumableHoldProvider.prompts), timeout=10
    )
    (worker,) = worker_chats(fx.host, SESSION)
    chat = str(worker["resource"])
    if drain:
        assert fx.chat in await fx.host.drain(0.2)
    await fx.shut()
    return chat


async def _restart(tmp_path: Path, node: str, *, drain: bool) -> tuple[dict[str, Any], list[str]]:
    """The resumed main turn and the worker chats the session then has."""
    ResumableHoldProvider.holding = False
    fx2 = await restart_flow(tmp_path, node, TOML)
    try:
        if not drain:
            await asyncio.sleep(0.3)
            await resume_by_hand(fx2)
        turn = await wait_resumed(fx2)
        return turn, [str(w["resource"]) for w in worker_chats(fx2.host, SESSION)]
    finally:
        await fx2.shut()


async def _resumed(tmp_path: Path, *, drain: bool) -> None:
    node = str(tmp_path / NO_NODE)
    chat = await _cut_off(tmp_path, node, "durinflight", PLAIN, drain=drain)
    turn, workers = await _restart(tmp_path, node, drain=drain)
    assert final_json(turn) == RESUMED_IN_PLACE
    assert workers == [chat]
    assert ResumableHoldProvider.prompts == ["hold one"]
    assert len(ResumableHoldProvider.turn_resumes) == 1
    assert ResumableHoldProvider.turn_resumes[0][0] == chat
    assert ResumableHoldProvider.sessions_resumed == 1
    assert any("in place" in line and chat in line for line in notifications(turn))


async def test_durinflight_resumed_once_after_drain(tmp_path: Path) -> None:
    await _resumed(tmp_path, drain=True)


async def test_durinflight_resumed_once_after_crash(tmp_path: Path) -> None:
    await _resumed(tmp_path, drain=False)


async def test_durinflight_schema_call_redispatched(tmp_path: Path) -> None:
    node = str(tmp_path / NO_NODE)
    await _cut_off(tmp_path, node, "durschema", SCHEMA, drain=True)
    await _restart(tmp_path, node, drain=True)
    assert ResumableHoldProvider.turn_resumes == []
    assert len(ResumableHoldProvider.prompts) == 2
    assert all(p.startswith("hold one") for p in ResumableHoldProvider.prompts)


async def test_durinflight_no_inner_state_sends_a_new_turn(tmp_path: Path) -> None:
    """Nothing recorded at the turn's start: the call is dispatched again, in a new chat."""
    node = str(tmp_path / NO_NODE)
    reset_fakes()
    ResumableHoldProvider.records_state = False
    chat = await _cut_off(tmp_path, node, "durinflight", PLAIN, drain=True, reset=False)
    turn, workers = await _restart(tmp_path, node, drain=True)
    assert final_json(turn) == "mem: hold one"
    assert ResumableHoldProvider.prompts == ["hold one", "hold one"]
    assert ResumableHoldProvider.turn_resumes == []
    assert ResumableHoldProvider.sessions_resumed == 0
    assert chat in workers
    assert len(workers) == 2


async def test_durinflight_session_without_resume_turn_sends_a_new_turn(tmp_path: Path) -> None:
    """The session resumes but cannot resume a turn: the prompt is sent again in the old chat."""
    node = str(tmp_path / NO_NODE)
    reset_fakes()
    ResumableHoldProvider.resumes_turns = False
    chat = await _cut_off(tmp_path, node, "durinflight", PLAIN, drain=True, reset=False)
    turn, workers = await _restart(tmp_path, node, drain=True)
    assert final_json(turn) == "mem: hold one"
    assert ResumableHoldProvider.prompts == ["hold one", "hold one"]
    assert ResumableHoldProvider.turn_resumes == []
    assert ResumableHoldProvider.sessions_resumed == 1
    assert workers == [chat]
    assert "agent() #0: provider 'rh' cannot resume a turn; dispatching it again" in notifications(
        turn
    )


async def test_durinflight_resume_session_failing_opens_a_new_chat(tmp_path: Path) -> None:
    """`resume_session` raises: the call is dispatched again in a new worker chat."""
    node = str(tmp_path / NO_NODE)
    chat = await _cut_off(tmp_path, node, "durinflight", PLAIN, drain=True)
    ResumableHoldProvider.fail_resume = True
    turn, workers = await _restart(tmp_path, node, drain=True)
    assert final_json(turn) == "mem: hold one"
    assert ResumableHoldProvider.prompts == ["hold one", "hold one"]
    assert ResumableHoldProvider.turn_resumes == []
    assert chat in workers
    assert len(workers) == 2
    assert any(
        "could not resume the cut-off turn (the old session is gone)" in line
        for line in notifications(turn)
    )


TIMED = (
    "meta = {'name': 'durtimed', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    try:\n"
    "        await agent('hold one', x={'timeout': 0.3})\n"
    "    except Exception:\n"
    "        pass\n"
    "    return await agent('hold two')\n"
)


async def test_durinflight_a_stop_the_run_made_is_not_kept_in_flight(tmp_path: Path) -> None:
    """A call the run's own timeout stopped is over: only the host-cut-off one stays in flight."""
    node = str(tmp_path / NO_NODE)
    reset_fakes()
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "durtimed.py").write_text(TIMED, encoding="utf-8")
    store = store_for(tmp_path)
    fx = await boot_flow(tmp_path, node, TOML, store=store)
    await start_turn(fx.wire, fx.chat, "t1", "/durtimed")
    assert await fx.wire.until(lambda: "hold two" in ResumableHoldProvider.prompts, timeout=10)
    assert await fx.wire.until(lambda: len(worker_chats(fx.host, SESSION)) == 2, timeout=10)
    first, second = (str(w["resource"]) for w in worker_chats(fx.host, SESSION))
    assert fx.chat in await fx.host.drain(0.2)
    await fx.shut()
    (stored,) = [s for s in await store_for(tmp_path).load_all() if s.uri == SESSION]
    assert stored.resume_state is not None
    (run,) = stored.resume_state["runs"]
    assert set(run["inflight"]) == {second}
    assert first not in run["inflight"]


REROUTED = (
    "meta = {'name': 'durroute', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await agent('hold one')\n"
    "    b = await agent('hold two', label='later')\n"
    "    return [a, b]\n"
)
# After the restart the cut-off call routes to another provider, and a later one to a holder.
TOML_REROUTED = (
    '[providers.rh]\nload = "tests.flow_fakes:ResumableHoldProvider"\n'
    '[providers.rh2]\nload = "tests.flow_fakes:ResumableHoldProvider"\n'
    '[providers.rh2.config]\nprovider_id = "rh2"\n'
    '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n'
    '[routing]\ndefault = "rh2"\nrules = [ { provider = "rec", label = "later" } ]\n'
)


async def test_durinflight_a_record_not_resumed_is_dropped(tmp_path: Path) -> None:
    """The cut-off call routes elsewhere after the restart and is dispatched again: its in-flight
    record is consumed then, not carried in the resume state for the rest of the run."""
    node = str(tmp_path / NO_NODE)
    chat = await _cut_off(tmp_path, node, "durroute", REROUTED, drain=True)
    ResumableHoldProvider.holding = False
    fx2 = await restart_flow(tmp_path, node, TOML_REROUTED)
    try:
        assert await fx2.wire.until(
            lambda: any(
                s.messages for p in flow_fakes.RecordingProvider.instances for s in p.sessions
            ),
            timeout=10,
        )
        flow = fx2.host._sessions[SESSION].agent_session
        assert isinstance(flow, FlowSession)
        (entry,) = flow._runs.values()
        assert entry.durable is not None
        assert chat not in entry.durable.inflight
        assert chat not in entry.durable.restored_inflight
        (held,) = (s for p in flow_fakes.RecordingProvider.instances for s in p.sessions)
        held.released.set()
        turn = await wait_resumed(fx2)
        assert final_json(turn) == ["mem: hold one", "rec: hold two"]
    finally:
        await fx2.shut()


async def test_durinflight_a_failure_after_resuming_closes_the_resumed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cut-off call's session is resumed, then its setup fails: that session is closed."""
    from ahp_host_flow.dispatch import ProviderDispatcher

    node = str(tmp_path / NO_NODE)
    await _cut_off(tmp_path, node, "durinflight", PLAIN, drain=True)
    closed_before = ResumableHoldProvider.sessions_closed  # the first host's, at shutdown

    def broken(self: Any, *args: Any) -> None:
        raise RuntimeError("no message for you")

    monkeypatch.setattr(ProviderDispatcher, "_build_message", broken)
    ResumableHoldProvider.holding = False
    fx2 = await restart_flow(tmp_path, node, TOML)
    try:
        await wait_resumed(fx2)
        assert ResumableHoldProvider.sessions_resumed == 1
        assert ResumableHoldProvider.sessions_closed == closed_before + 1
    finally:
        await fx2.shut()
