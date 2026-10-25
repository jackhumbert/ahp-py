"""Redacting one `agent()` call of a real run: worker chat, main chat and journal."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_host import Host
from ahp_protocol.ext import flow

from ahp_host_flow.redaction import redact_step
from tests.flow_fakes import RestartHoldProvider, reset_fakes
from tests.flow_hosting import (
    SESSION,
    boot_flow,
    descriptors,
    final_json,
    restart_flow,
    store_for,
)
from tests.hosting import found, run_turn, start_turn, state, worker_chats

pytestmark = pytest.mark.node

SECRET = "PLAINLEAK-4242"
TOML = """
[providers.echo]
load = "tests.flow_fakes:StrippedEchoProvider"

[routing]
default = "echo"
"""

ACTOR = "test operator"
HOLD_TOML = (
    '[providers.hold]\nload = "tests.flow_fakes:RestartHoldProvider"\n'
    '\n[routing]\ndefault = "hold"\n'
)
# Two calls: the first prints the secret (assembled at run time, so neither the
# script text nor the journal header holds the literal), the second is held so a
# crash can cut the run off after the first finished.
HOLD_SCRIPT = """```js
const a = await agent('say ' + ['PLAIN','LEAK-4242'].join(''), {label: 'leak'});
const c = await agent('hold three');
return {a, c}
```"""

SCRIPT = """```js
const r = await agent('say ' + ['PLAIN','LEAK-4242'].join(''), {label: 'leak'});
return 'done'
```"""


def on_disk(directory: Path) -> str:
    """Every file under *directory*, as text (the store file, a journal)."""
    text = ""
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            text += path.read_text(encoding="utf-8", errors="replace")
    return text


async def redact_prompts(host: Host) -> int:
    """Redact the worker turns' user messages (the prompt is not part of `redact_step`)."""
    done = 0
    for chat in worker_chats(host, SESSION):
        uri = str(chat["resource"])
        for turn in list(state(host, uri).get("turns", [])):
            if SECRET in json.dumps(turn.get("message")):
                await host.redact_message(uri, str(turn["id"]), "leak", actor=ACTOR)
                done += 1
    return done


async def test_flow_redaction_run(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, TOML, store=store_for(tmp_path))
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", SCRIPT)
        # Proves the secret was stored before, so its absence below means something
        # (waited out, not polled: a reader can make the store's replace fail on Windows).
        await asyncio.sleep(0.5)
        assert SECRET in on_disk(tmp_path / "sessions")
        assert turn["state"] == "complete"
        run_id = next(d["runStarted"]["runId"] for d in descriptors(turn) if "runStarted" in d)

        report = await redact_step(
            fx.host,
            state_dir=tmp_path / "state",
            run_id=run_id,
            ordinal=0,
            reason="leak",
            actor=ACTOR,
        )
        assert report.main_chat == fx.chat
        assert report.worker_chat is not None

        worker_state = state(fx.host, report.worker_chat)
        parts = [p for t in worker_state["turns"] for p in t["responseParts"]]
        assert SECRET not in json.dumps(parts)
        # The prompt is the turn's message, which redaction does not touch.
        assert SECRET in json.dumps([t["message"] for t in worker_state["turns"]])
        assert await redact_prompts(fx.host) == 1
        main = state(fx.host, fx.chat)
        calls = found(main, lambda m: m.get("toolName") == "agent")
        assert calls
        assert SECRET not in json.dumps(calls)
        assert flow.run_view(main).steps[0].result == "[redacted]"
        journal = tmp_path / "state" / "runs" / run_id / "journal.jsonl"
        assert SECRET not in journal.read_text(encoding="utf-8")
        # The stored session (main chat and worker chat) holds no trace: the
        # redaction wrote it before returning, with no debounce to wait out.
        stored = on_disk(tmp_path / "sessions")
        assert fx.chat in stored
        assert report.worker_chat is not None
        assert report.worker_chat in stored
        assert SECRET not in stored

        await fx.wire.request("unsubscribe", {"channel": fx.chat})
        snapshot = await fx.wire.request("subscribe", {"channel": fx.chat})
        assert SECRET not in json.dumps(snapshot)
    finally:
        await fx.shut()


def last_turn(host: Host, chat: str) -> Mapping[str, Any]:
    return dict(state(host, chat)["turns"][-1])


async def test_flow_redaction_survives_restart_and_resume(tmp_path: Path, node_path: str) -> None:
    """Redact after a crash, restart again, resume the cut-off run: it replays `[redacted]`."""
    reset_fakes()
    sessions = tmp_path / "sessions"
    fx = await boot_flow(tmp_path, node_path, HOLD_TOML, store=store_for(tmp_path))
    await start_turn(fx.wire, fx.chat, "t1", HOLD_SCRIPT)
    assert await fx.wire.until(lambda: "hold three" in RestartHoldProvider.prompts)
    await fx.shut()  # the crash: the first call finished, the second was in flight
    assert SECRET in on_disk(sessions)
    assert SECRET in on_disk(tmp_path / "state")

    RestartHoldProvider.holding = False
    fx2 = await restart_flow(tmp_path, node_path, HOLD_TOML)
    try:
        await asyncio.sleep(0.3)
        failed = last_turn(fx2.host, fx2.chat)
        assert (failed["id"], failed["state"]) == ("t1", "error")
        assert failed["responseParts"][-1]["resumable"] is True
        run_id = next(d["runStarted"]["runId"] for d in descriptors(failed) if "runStarted" in d)

        report = await redact_step(
            fx2.host,
            state_dir=tmp_path / "state",
            run_id=run_id,
            ordinal=0,
            reason="leak",
            actor=ACTOR,
        )
        assert report.worker_chat is not None
        assert await redact_prompts(fx2.host) == 1
        # Both chats' stored files and the journal: no trace.
        stored = on_disk(sessions)
        assert fx2.chat in stored
        assert report.worker_chat in stored
        assert SECRET not in stored
        assert SECRET not in on_disk(tmp_path / "state")
    finally:
        await fx2.shut()

    # A second restart: what is stored is what the next process loads.
    fx3 = await restart_flow(tmp_path, node_path, HOLD_TOML)
    try:
        await asyncio.sleep(0.3)
        assert SECRET not in json.dumps(state(fx3.host, SESSION))
        assert SECRET not in json.dumps(state(fx3.host, fx3.chat))
        failed = last_turn(fx3.host, fx3.chat)
        assert (failed["id"], failed["state"]) == ("t1", "error")
        assert failed["responseParts"][-1]["resumable"] is True

        # What a client does with a turn a crash failed: `chat/turnResume`.
        await fx3.wire.dispatch(fx3.chat, {"type": "chat/turnResume", "turnId": "t1"})

        def resumed() -> bool:
            chat_state = state(fx3.host, fx3.chat)
            turns = chat_state.get("turns", [])
            return (
                chat_state.get("activeTurn") is None
                and bool(turns)
                and turns[-1].get("state") == "complete"
            )

        assert await fx3.wire.until(resumed, timeout=20.0), state(fx3.host, fx3.chat)
        turn = last_turn(fx3.host, fx3.chat)
        result = final_json(turn)
        assert result["a"] == "[redacted]"  # replayed from the redacted journal
        assert result["c"] == "rec: hold three"
        # The redacted call was not dispatched again; only the held one was.
        assert RestartHoldProvider.prompts.count("say " + SECRET) == 1
        assert RestartHoldProvider.prompts.count("hold three") == 2
        assert SECRET not in json.dumps(state(fx3.host, fx3.chat))
        for chat in worker_chats(fx3.host, SESSION):
            assert SECRET not in json.dumps(state(fx3.host, str(chat["resource"])))
    finally:
        await fx3.shut()
    # The resumed run's own journal and the final store: still nothing.
    assert SECRET not in on_disk(tmp_path / "state")
    assert SECRET not in on_disk(sessions)


RETRY_TOML = (
    '[providers.first]\nload = "tests.flow_fakes:EmptyFirstProvider"\nretry_empty = 1\n'
    '\n[routing]\ndefault = "first"\n'
)


def worker_parts(host: Host, chat: str) -> str:
    """Every response part of *chat*, as JSON (not the turns' messages, the prompts)."""
    return json.dumps([p for t in state(host, chat)["turns"] for p in t["responseParts"]])


async def test_flow_redaction_every_attempt(tmp_path: Path, node_path: str) -> None:
    """A retried call is redacted on every attempt's tool call and worker chat."""
    reset_fakes()
    fx = await boot_flow(tmp_path, node_path, RETRY_TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", SCRIPT)
        assert turn["state"] == "complete"
        run_id = next(d["runStarted"]["runId"] for d in descriptors(turn) if "runStarted" in d)
        calls = found(state(fx.host, fx.chat), lambda m: m.get("toolName") == "agent")
        assert {c["toolCallId"] for c in calls} == {"agent-0", "agent-0-a1"}
        chats = [str(c["resource"]) for c in worker_chats(fx.host, SESSION)]
        assert len(chats) == 2
        for chat in chats:  # the first attempt thought it, the second said it
            assert SECRET in worker_parts(fx.host, chat)

        report = await redact_step(
            fx.host,
            state_dir=tmp_path / "state",
            run_id=run_id,
            ordinal=0,
            reason="leak",
            actor=ACTOR,
        )
        for chat in chats:
            assert SECRET not in worker_parts(fx.host, chat)
        calls = found(state(fx.host, fx.chat), lambda m: m.get("toolName") == "agent")
        assert len(calls) == 2
        assert SECRET not in json.dumps(calls)
        assert set(report.tool_call_ids) == {"agent-0", "agent-0-a1"}
        assert set(report.worker_chats) == set(chats)
    finally:
        await fx.shut()
