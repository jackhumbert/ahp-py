"""A client continues an ended run in a new turn (`continueRun`), from any chat of the host.

The run is cancelled after its first call finished; the journal stays, and a later turn on
another chat or session replays the finished call and runs the rest under a new run id.
Only the principal the journal recorded may continue it. Fakes only: no live model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.ext import flow

from ahp_host_flow.engine import load_journal
from ahp_host_flow.provider import RunEntry
from tests.flow_fakes import RestartHoldProvider, reset_fakes
from tests.flow_hosting import FlowFixture, boot_flow, descriptors, final_json
from tests.hosting import (
    Wire,
    cancel_turn,
    connect,
    open_session,
    start_turn,
    state,
    wait_turn,
)
from tests.integration.test_flowprincipal import HeaderPolicy

TOML = (
    '[providers.hold]\nload = "tests.flow_fakes:RestartHoldProvider"\n'
    '\n[routing]\ndefault = "hold"\n'
)
THREE_PY = (
    "meta = {'name': 'three', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await agent('one')\n"
    "    b = await agent('hold two')\n"
    "    c = await agent('three')\n"
    "    return {'a': a, 'b': b, 'c': c}\n"
)
EXPECTED = {"a": "rec: one", "b": "rec: hold two", "c": "rec: three"}
ALICE = {"x-forwarded-user": "alice"}


def write_workflow(base: Path) -> None:
    (base / "user").mkdir(parents=True, exist_ok=True)
    (base / "user" / "three.py").write_text(THREE_PY, encoding="utf-8")


async def boot(base: Path, toml: str = TOML, **boot_args: Any) -> FlowFixture:
    reset_fakes()
    write_workflow(base)
    return await boot_flow(base, str(base / "no-node"), toml, **boot_args)


async def cancelled_run(fx: FlowFixture, wire: Wire, chat: str, turn_id: str = "t1") -> str:
    """Start `three` on *chat*, cancel it while its second call is held; the run's id."""
    before = len(RestartHoldProvider.prompts)
    await start_turn(wire, chat, turn_id, "/three")
    assert await wire.until(lambda: "hold two" in RestartHoldProvider.prompts[before:])
    await cancel_turn(wire, chat, turn_id)
    turn = await wait_turn(wire, chat, turn_id)
    assert turn["state"] == "cancelled"
    started = [d["runStarted"]["runId"] for d in descriptors(turn) if "runStarted" in d]
    assert len(started) == 1
    RestartHoldProvider.holding = False
    return str(started[0])


async def second_chat(fx: FlowFixture, wire: Wire | None = None, uri: str = "flow:/s2") -> str:
    return await open_session(
        wire or fx.wire, uri, "flow", working_directories=[fx.workspace.as_uri()]
    )


async def continue_turn(wire: Wire, chat: str, turn_id: str, run_id: str) -> dict[str, Any]:
    await start_turn(wire, chat, turn_id, "/continue", meta=flow.meta(flow.continue_start(run_id)))
    return await wait_turn(wire, chat, turn_id)


def failure(turn: dict[str, Any]) -> str:
    assert turn["state"] == "error", turn
    part = turn["responseParts"][-1]["error"]
    assert part["errorType"] == "flow.run"
    assert not turn["responseParts"][-1].get("resumable")
    return str(part["message"])


async def test_continue_replays_finished_calls_from_a_new_session(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        run = await cancelled_run(fx, fx.wire, fx.chat)
        other = await second_chat(fx)
        RestartHoldProvider.prompts.clear()
        turn = await continue_turn(fx.wire, other, "c1", run)
        assert turn["state"] == "complete", turn
        assert final_json(turn) == EXPECTED
        # Call one replayed from the journal, calls two and three dispatched.
        assert RestartHoldProvider.prompts == ["hold two", "three"]
        started = [d["runStarted"]["runId"] for d in descriptors(turn) if "runStarted" in d]
        assert len(started) == 1
        assert started[0] != run
        journal = load_journal(tmp_path / "state", started[0])
        assert journal.header.resumed_from == run
        assert journal.header.principal is None
        # The source run's own journal is untouched.
        assert load_journal(tmp_path / "state", run).header.resumed_from is None
        assert state(fx.host, other)["turns"][-1]["id"] == "c1"
    finally:
        await fx.shut()


async def test_continue_of_a_continued_run_chains(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        run = await cancelled_run(fx, fx.wire, fx.chat)
        other = await second_chat(fx)
        RestartHoldProvider.holding = True
        before = len(RestartHoldProvider.prompts)
        await start_turn(
            fx.wire, other, "c1", "/continue", meta=flow.meta(flow.continue_start(run))
        )
        assert await fx.wire.until(lambda: "hold two" in RestartHoldProvider.prompts[before:])
        await cancel_turn(fx.wire, other, "c1")
        cut = await wait_turn(fx.wire, other, "c1")
        second = next(d["runStarted"]["runId"] for d in descriptors(cut) if "runStarted" in d)
        RestartHoldProvider.holding = False
        third = await second_chat(fx, uri="flow:/s3")
        RestartHoldProvider.prompts.clear()
        turn = await continue_turn(fx.wire, third, "c2", second)
        assert final_json(turn) == EXPECTED
        assert RestartHoldProvider.prompts == ["hold two", "three"]
        final = next(d["runStarted"]["runId"] for d in descriptors(turn) if "runStarted" in d)
        assert load_journal(tmp_path / "state", final).header.resumed_from == second
    finally:
        await fx.shut()


async def test_continue_unknown_run_is_refused_without_dispatch(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        RestartHoldProvider.holding = False
        turn = await continue_turn(fx.wire, fx.chat, "c1", "does-not-exist")
        assert "no run 'does-not-exist' can be continued" in failure(turn)
        assert RestartHoldProvider.prompts == []
    finally:
        await fx.shut()


async def test_continue_a_live_run_is_refused(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/three")
        assert await fx.wire.until(lambda: "hold two" in RestartHoldProvider.prompts)
        (live,) = [p.name for p in (tmp_path / "state" / "runs").iterdir()]
        other = await second_chat(fx)
        turn = await continue_turn(fx.wire, other, "c1", live)
        assert "still running" in failure(turn)
        assert RestartHoldProvider.prompts == ["one", "hold two"]
        RestartHoldProvider.holding = False
        await cancel_turn(fx.wire, fx.chat, "t1")
        await wait_turn(fx.wire, fx.chat, "t1")
    finally:
        await fx.shut()


async def test_continue_refuses_a_malformed_start(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        meta = flow.meta({"continueRun": "abc", "args": {"x": 1}})
        await start_turn(fx.wire, fx.chat, "c1", "/continue", meta=meta)
        turn = await wait_turn(fx.wire, fx.chat, "c1")
        assert turn["state"] == "error"
        assert "cannot be combined with args" in json.dumps(turn)
        assert RestartHoldProvider.prompts == []
    finally:
        await fx.shut()


async def test_continue_a_corrupt_journal_is_refused(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        run = await cancelled_run(fx, fx.wire, fx.chat)
        journal = tmp_path / "state" / "runs" / run / "journal.jsonl"
        with journal.open("a", encoding="utf-8") as f:
            f.write("not json\n")
        RestartHoldProvider.prompts.clear()
        turn = await continue_turn(fx.wire, await second_chat(fx), "c1", run)
        assert "journal cannot be read" in failure(turn)
        assert RestartHoldProvider.prompts == []
    finally:
        await fx.shut()


async def test_continue_a_masked_journal_is_refused(tmp_path: Path) -> None:
    fx = await boot(tmp_path, TOML + '\n[redact]\npatterns = ["two"]\n')
    try:
        run = await cancelled_run(fx, fx.wire, fx.chat)
        RestartHoldProvider.prompts.clear()
        turn = await continue_turn(fx.wire, await second_chat(fx), "c1", run)
        assert "masked" in failure(turn)
        assert RestartHoldProvider.prompts == []
    finally:
        await fx.shut()


async def test_continue_can_be_switched_off(tmp_path: Path) -> None:
    fx = await boot(tmp_path, TOML + "\n[resume]\ncontinue = false\n")
    try:
        run = await cancelled_run(fx, fx.wire, fx.chat)
        RestartHoldProvider.prompts.clear()
        turn = await continue_turn(fx.wire, await second_chat(fx), "c1", run)
        assert "switched off" in failure(turn)
        assert RestartHoldProvider.prompts == []
    finally:
        await fx.shut()


async def test_continue_needs_the_same_principal(tmp_path: Path) -> None:
    fx = await boot(tmp_path, policy=HeaderPolicy(), headers=ALICE)
    bob, bob_serving = await connect(fx.host, headers={"x-forwarded-user": "bob"}, client_id="c2")
    try:
        run = await cancelled_run(fx, fx.wire, fx.chat)
        assert load_journal(tmp_path / "state", run).header.principal == "alice"
        RestartHoldProvider.prompts.clear()
        bobs = await second_chat(fx, bob, "flow:/bob")
        refused = failure(await continue_turn(bob, bobs, "c1", run))
        assert "alice" not in refused
        assert refused == failure(await continue_turn(bob, bobs, "c2", "does-not-exist")).replace(
            "does-not-exist", run
        )
        assert RestartHoldProvider.prompts == []
        # Alice, from another session, continues it, and the new run is still hers.
        turn = await continue_turn(fx.wire, await second_chat(fx), "c3", run)
        assert final_json(turn) == EXPECTED
        new = next(d["runStarted"]["runId"] for d in descriptors(turn) if "runStarted" in d)
        assert load_journal(tmp_path / "state", new).header.principal == "alice"
    finally:
        await bob.close()
        bob_serving.cancel()
        await fx.shut()


@pytest.mark.parametrize("owner_is_anonymous", [True, False])
async def test_continue_with_and_without_a_recorded_principal(
    tmp_path: Path, owner_is_anonymous: bool
) -> None:
    """A run with no principal is continued only by a caller with none, and the reverse."""
    fx = await boot(tmp_path, policy=HeaderPolicy(), headers=ALICE)
    anon, anon_serving = await connect(fx.host, client_id="c2")
    try:
        owner, other = (anon, fx.wire) if owner_is_anonymous else (fx.wire, anon)
        await anon.request("subscribe", {"channel": "flow:/s1"})
        await anon.request("subscribe", {"channel": fx.chat})
        chat = fx.chat if owner is fx.wire else await second_chat(fx, anon, "flow:/anon")
        run = await cancelled_run(fx, owner, chat)
        assert load_journal(tmp_path / "state", run).header.principal == (
            None if owner_is_anonymous else "alice"
        )
        RestartHoldProvider.prompts.clear()
        stranger_chat = await second_chat(fx, other, "flow:/other")
        assert "can be continued" in failure(await continue_turn(other, stranger_chat, "c1", run))
        assert RestartHoldProvider.prompts == []
        again = await second_chat(fx, owner, "flow:/again")
        assert final_json(await continue_turn(owner, again, "c2", run)) == EXPECTED
    finally:
        await anon.close()
        anon_serving.cancel()
        await fx.shut()


async def test_continue_refusals_never_reach_a_foreign_caller(tmp_path: Path) -> None:
    """Live, restart-pending, corrupt and masked runs all answer a stranger as an unknown id."""
    fx = await boot(tmp_path, policy=HeaderPolicy(), headers=ALICE)
    bob, bob_serving = await connect(fx.host, headers={"x-forwarded-user": "bob"}, client_id="c2")
    try:
        bobs = await second_chat(fx, bob, "flow:/bob")
        mine = await second_chat(fx)
        n = 0

        async def asks(wire: Wire, chat: str, run: str) -> str:
            nonlocal n
            n += 1
            return failure(await continue_turn(wire, chat, f"x{n}", run))

        def unknown(run: str) -> str:
            return f"no run {run!r} can be continued by this caller"

        # Live: only the owner hears "still running".
        await start_turn(fx.wire, fx.chat, "t1", "/three")
        assert await fx.wire.until(lambda: "hold two" in RestartHoldProvider.prompts)
        (live,) = [p.name for p in (tmp_path / "state" / "runs").iterdir()]
        assert await asks(bob, bobs, live) == unknown(live)
        assert "still running" in await asks(fx.wire, mine, live)
        RestartHoldProvider.holding = False
        await cancel_turn(fx.wire, fx.chat, "t1")
        await wait_turn(fx.wire, fx.chat, "t1")
        assert RestartHoldProvider.prompts == ["one", "hold two"]

        # Restart-pending counts as live, owner first.
        session = next(iter(fx.host.provider._sessions))  # type: ignore[attr-defined]
        entry = RunEntry(fx.chat, "t1", live, None, None, 0, 0, 0, "alice")
        session._restored[fx.chat] = entry
        assert await asks(bob, bobs, live) == unknown(live)
        assert "still running" in await asks(fx.wire, mine, live)
        session._restored.clear()

        # Corrupt below the header: the owner hears it, a stranger does not.
        journal = tmp_path / "state" / "runs" / live / "journal.jsonl"
        good = journal.read_text(encoding="utf-8")
        journal.write_text(good + "not json\n", encoding="utf-8")
        assert await asks(bob, bobs, live) == unknown(live)
        assert "journal cannot be read" in await asks(fx.wire, mine, live)
        # No readable header: nobody can be shown to own it.
        journal.write_text("not json\n", encoding="utf-8")
        assert await asks(bob, bobs, live) == unknown(live)
        assert await asks(fx.wire, mine, live) == unknown(live)
        journal.write_text(good, encoding="utf-8")

        # Masked: same.
        masked = json.loads(good.splitlines()[0])
        masked["source"] = "[redacted]"
        journal.write_text(
            json.dumps(masked) + "\n" + "\n".join(good.splitlines()[1:]) + "\n", encoding="utf-8"
        )
        assert await asks(bob, bobs, live) == unknown(live)
        assert "masked" in await asks(fx.wire, mine, live)
        assert RestartHoldProvider.prompts == ["one", "hold two"]
    finally:
        await bob.close()
        bob_serving.cancel()
        await fx.shut()


async def _turn_with_model(
    wire: Wire, chat: str, turn_id: str, text: str, model: str, meta: dict[str, Any] | None = None
) -> None:
    message: dict[str, Any] = {"text": text, "origin": {"kind": "user"}, "model": {"id": model}}
    if meta is not None:
        message["_meta"] = meta
    await wire.dispatch(
        chat,
        {
            "type": "chat/turnStarted",
            "turnId": turn_id,
            "startedAt": "1970-01-01T00:00:01.000Z",
            "message": message,
        },
    )


async def test_continue_runs_with_the_original_runs_model(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        await _turn_with_model(fx.wire, fx.chat, "t1", "/three", "model-a")
        assert await fx.wire.until(lambda: "hold two" in RestartHoldProvider.prompts)
        await cancel_turn(fx.wire, fx.chat, "t1")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        run = next(d["runStarted"]["runId"] for d in descriptors(turn) if "runStarted" in d)
        RestartHoldProvider.holding = False
        assert load_journal(tmp_path / "state", run).header.model == "model-a"
        other = await second_chat(fx)
        meta = flow.meta(flow.continue_start(run))
        await _turn_with_model(fx.wire, other, "c1", "/continue", "model-b", meta)
        done = await wait_turn(fx.wire, other, "c1")
        new = next(d["runStarted"]["runId"] for d in descriptors(done) if "runStarted" in d)
        assert load_journal(tmp_path / "state", new).header.model == "model-a"
    finally:
        await fx.shut()
