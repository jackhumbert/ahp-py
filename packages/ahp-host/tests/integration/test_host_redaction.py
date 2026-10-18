"""Redaction after publishing: `Host.redact` / `Host.redact_message` (ADR 0007).

The property is not "a marker appeared" -- it is that the original content is
unobtainable afterwards by every route a client or an operator has: the live
stream, a resubscribe, a reconnect replay, the store file, the audit log. One
route is documented as NOT closed: wire-log lines written before the redaction.

Secret checks are exact substring searches with no tolerance.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.ext.redaction import PART_REDACTED, part_id_of
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy, RedactionError
from ahp_host.core.audit import AuditEvent
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider
from tests.conformance.schemas import assert_valid_state
from tests.unit.hosting import Wire, connect, finished, open_session, run_turn, state

pytestmark = pytest.mark.anyio

SECRET = "PLAINLEAK-4242"
SECRET_TEXT = f"the secret is {SECRET}"
EXTENSION = "ahp-py/redaction"
URI = "echo:/red-1"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


async def _wire(host: Host, client_id: str, *, opted: bool) -> tuple[Wire, asyncio.Task[None]]:
    """A client that does (or does not) advertise the redaction extension."""
    if not opted:
        return await connect(host, client_id)
    client, server = memory_pair()
    serving = asyncio.create_task(host.serve(server))
    wire = Wire(host, client)
    response = await wire.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["1.0.0"],
            "initialSubscriptions": [ROOT_URI],
            "_meta": {EXTENSION: {}},
        },
    )
    assert "result" in response, response
    wire.initialized = dict(response["result"])
    return wire, serving


async def _fresh(host: Host) -> tuple[Wire, asyncio.Task[None]]:
    """A new transport with nothing sent on it yet (a reconnect is its first request)."""
    client, server = memory_pair()
    return Wire(host, client), asyncio.create_task(host.serve(server))


class Scenario:
    def __init__(self, host: Host) -> None:
        self.host = host
        self.tasks: list[asyncio.Task[None]] = []
        self.wires: list[Wire] = []
        self.chat = ""
        self.stock: Wire
        self.opted: Wire
        self.seq_before_secret = 0

    async def start(self) -> Scenario:
        self.stock, serving = await _wire(self.host, "stock", opted=False)
        self.opted, opted_serving = await _wire(self.host, "opted", opted=True)
        self.tasks += [serving, opted_serving]
        self.wires += [self.stock, self.opted]
        self.chat = await open_session(self.stock, URI)
        await self.opted.request("subscribe", {"channel": URI})
        await self.opted.request("subscribe", {"channel": self.chat})
        # The first turn is benign so the session title the host seeds from it
        # holds no secret.
        await run_turn(self.stock, self.chat, "t1", "first")
        self.seq_before_secret = self.host.sequencer.server_seq
        await run_turn(self.stock, self.chat, "t2", SECRET_TEXT)
        return self

    def part_id(self, turn_id: str = "t2") -> str:
        turn = next(t for t in state(self.host, self.chat)["turns"] if t["id"] == turn_id)
        part = next(p for p in turn["responseParts"] if p["kind"] == "markdown")
        found = part_id_of(part)
        assert isinstance(found, str)
        return found

    async def close(self) -> None:
        for wire in self.wires:
            await wire.close()
        for task in self.tasks:
            task.cancel()
        await self.host.aclose()


class FlakyStore(FileSessionStore):
    """A real file store whose writes can be made to fail (an unwritable disk)."""

    failing = False

    async def save(self, session: Any) -> None:
        if self.failing:
            raise OSError("disk full")
        await super().save(session)

    async def save_soon(self, session: Any) -> None:
        if self.failing:
            raise OSError("disk full")
        await super().save_soon(session)


def _host(**kwargs: Any) -> Host:
    return Host(EchoProvider(), LoopbackSingleUserPolicy(), **kwargs)


def _dump(value: Any) -> str:
    return json.dumps(value, default=str)


def _on_disk(directory: Path) -> str:
    text = ""
    for path in directory.rglob("*"):
        if path.is_file():
            try:
                text += path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return text


def _max_seq(wire: Wire) -> int:
    return int(max(e["serverSeq"] for e in wire.envelopes() if "serverSeq" in e))


async def test_host_redaction_is_live_for_opted_in_and_never_unknown_to_stock() -> None:
    host = _host()
    scenario = await Scenario(host).start()
    try:
        chat, stock, opted = scenario.chat, scenario.stock, scenario.opted
        for wire in (stock, opted):
            assert wire.initialized["_meta"] == {EXTENSION: {}}
        part_id = scenario.part_id()

        envelope = await host.redact(chat, "t2", part_id, "leaked", actor="ops")

        assert await opted.until(lambda: bool(opted.actions(chat, PART_REDACTED)))
        live = [e for e in opted.envelopes(chat) if e["action"]["type"] == PART_REDACTED]
        assert [e["serverSeq"] for e in live] == [envelope["serverSeq"]]
        assert live[0]["action"]["reason"] == "leaked"
        turn = next(t for t in state(host, chat)["turns"] if t["id"] == "t2")
        part = next(p for p in turn["responseParts"] if part_id_of(p) == part_id)
        assert part["content"] == "[redacted]"
        assert_valid_state("chat", state(host, chat))

        await run_turn(stock, chat, "t3", "later")
        assert await stock.until(
            lambda: any(
                a["type"] == "chat/turnComplete" and a.get("turnId") == "t3"
                for a in stock.actions(chat)
            )
        )
        # Per-connection ordering: everything the host sent the stock client
        # after the redaction has arrived, and none of it was an extension.
        assert [a["type"] for a in stock.actions() if a["type"].startswith("ahp-py/")] == []
    finally:
        await scenario.close()


async def test_host_redaction_stock_resubscribe_gets_the_redacted_state() -> None:
    host = _host()
    scenario = await Scenario(host).start()
    try:
        await host.redact(scenario.chat, "t2", scenario.part_id(), "leaked", actor="ops")
        stock = scenario.stock
        await stock.request("unsubscribe", {"channel": scenario.chat})
        response = await stock.request("subscribe", {"channel": scenario.chat})
        snapshot = response["result"]["snapshot"]
        turn = next(t for t in snapshot["state"]["turns"] if t["id"] == "t2")
        assert [p["content"] for p in turn["responseParts"] if p["kind"] == "markdown"] == [
            "[redacted]"
        ]
        # The part only: the message was not redacted in this test and still
        # carries the text, as it should.
        assert SECRET not in _dump(turn["responseParts"])
    finally:
        await scenario.close()


async def test_host_redaction_replay_never_resends_the_original() -> None:
    host = _host()
    scenario = await Scenario(host).start()
    try:
        chat = scenario.chat
        await host.redact(chat, "t2", scenario.part_id(), "leaked", actor="ops")
        await host.redact_message(chat, "t2", "leaked", actor="ops")
        await run_turn(scenario.stock, chat, "t3", "later")
        assert await scenario.stock.until(
            lambda: any(a.get("turnId") == "t3" for a in scenario.stock.actions(chat))
        )
        latest = host.sequencer.server_seq

        async def reconnect(client_id: str, last_seen: int, *, opted: bool) -> dict[str, Any]:
            wire, serving = await _fresh(host)
            scenario.wires.append(wire)
            scenario.tasks.append(serving)
            params: dict[str, Any] = {
                "channel": ROOT_URI,
                "clientId": client_id,
                "lastSeenServerSeq": last_seen,
                "subscriptions": [chat],
            }
            if opted:
                params["_meta"] = {EXTENSION: {}}
            response = await wire.request("reconnect", params)
            assert "result" in response, response
            result: dict[str, Any] = response["result"]
            assert SECRET not in _dump(result)
            return result

        # The stock client was never sent the redaction, so it is owed a snapshot
        # even though it claims to be up to date.
        assert (await reconnect("stock", _max_seq(scenario.stock), opted=False))[
            "type"
        ] == "snapshot"
        assert (await reconnect("opted", scenario.seq_before_secret, opted=True))[
            "type"
        ] == "snapshot"
        assert (await reconnect("opted", latest, opted=True))["type"] == "replay"

        assert SECRET not in _dump(list(host.sequencer._log.get(chat, [])))
        for channel in host.sequencer.channels():
            assert SECRET not in _dump(host.sequencer.state_of(channel))
    finally:
        await scenario.close()


async def test_host_redaction_store_and_audit_never_hold_the_original(tmp_path: Path) -> None:
    sink = ListSink()
    directory = tmp_path / "sessions"
    host = _host(
        store=FileSessionStore(directory, debounce=0.05),
        sequence_file=tmp_path / "seq",
        audit=sink,
    )
    scenario = await Scenario(host).start()
    try:
        chat = scenario.chat
        part_id = scenario.part_id()
        # Proves the secret was persisted before, so its absence below means
        # something. It waits out the debounce instead of polling the directory:
        # on Windows a reader holding a file open makes the store's os.replace
        # fail with WinError 32 (measured), leaving a stale .tmp copy of the old
        # state, so polling would make this test measure its own interference.
        await asyncio.sleep(0.5)
        assert SECRET in _on_disk(directory)

        await host.redact(chat, "t2", part_id, "leaked", actor="ops")
        await host.redact_message(chat, "t2", "leaked", actor="ops")

        # No flush: the write was immediate.
        assert SECRET not in _on_disk(directory)
        events = [e for e in sink.events if e.kind == "chat.redacted"]
        assert len(events) == 2
        assert {e.reason for e in events} == {"leaked"}
        by_target = {("partId" in e.detail): e.detail for e in events}
        assert by_target[True]["turnId"] == "t2"
        assert by_target[True]["partId"] == part_id
        assert by_target[False]["target"] == "message"
        for event in events:
            assert event.channel == chat
            assert event.detail["actor"] == "ops"
            assert event.detail["redactedAt"]
            assert isinstance(event.detail["serverSeq"], int)
        assert SECRET not in repr(sink.events)
    finally:
        await scenario.close()


async def test_host_redaction_wire_log_keeps_earlier_frames_only(tmp_path: Path) -> None:
    log = tmp_path / "wire.jsonl"
    host = _host(wire_log=log)
    scenario = await Scenario(host).start()
    try:
        chat, stock, opted = scenario.chat, scenario.stock, scenario.opted
        assert await stock.until(
            lambda: any(
                a["type"] == "chat/turnComplete" and a.get("turnId") == "t2"
                for a in stock.actions(chat)
            )
        )
        n = len(log.read_text(encoding="utf-8").splitlines())

        await host.redact(chat, "t2", scenario.part_id(), "leaked", actor="ops")
        await host.redact_message(chat, "t2", "leaked", actor="ops")
        await stock.request("unsubscribe", {"channel": chat})
        await stock.request("subscribe", {"channel": chat})
        assert await opted.until(lambda: bool(opted.actions(chat, PART_REDACTED)))

        lines = log.read_text(encoding="utf-8").splitlines()
        # Documented (core/wirelog.py): an append-only transcript is not rewritten.
        assert SECRET in "\n".join(lines[:n])
        assert SECRET not in "\n".join(lines[n:])
        assert PART_REDACTED in "\n".join(lines[n:])
    finally:
        await scenario.close()


async def test_host_redaction_refuses_targets_that_are_not_there() -> None:
    host = _host()
    scenario = await Scenario(host).start()
    try:
        chat, part_id = scenario.chat, scenario.part_id()
        with pytest.raises(RedactionError):
            await host.redact("echo:/nowhere", "t2", part_id, "x", actor="ops")
        with pytest.raises(RedactionError, match="not a chat channel"):
            await host.redact(URI, "t2", part_id, "x", actor="ops")
        with pytest.raises(RedactionError, match="turn"):
            await host.redact(chat, "t-nope", part_id, "x", actor="ops")
        with pytest.raises(RedactionError, match="part"):
            await host.redact(chat, "t2", "p-nope", "x", actor="ops")
        with pytest.raises(RedactionError, match="turn"):
            await host.redact_message(chat, "t-nope", "x", actor="ops")
        with pytest.raises(RedactionError, match="not a chat channel"):
            await host.redact_message(URI, "t2", "x", actor="ops")
        # Nothing was published by any of them.
        assert SECRET in _dump(state(host, chat))
    finally:
        await scenario.close()


async def test_host_redaction_cannot_be_dispatched_by_a_client() -> None:
    host = _host()
    scenario = await Scenario(host).start()
    try:
        chat, stock = scenario.chat, scenario.stock
        before = _dump(state(host, chat))
        client_seq = await stock.dispatch(
            chat,
            {
                "type": PART_REDACTED,
                "turnId": "t2",
                "partId": scenario.part_id(),
                "reason": "x",
                "redactedAt": "1970-01-01T00:00:00.000Z",
            },
        )
        echo = await stock.echoed(chat, client_seq)
        assert echo.get("rejectionReason")
        assert _dump(state(host, chat)) == before
    finally:
        await scenario.close()


async def test_host_redaction_discards_stored_content_the_part_referenced() -> None:
    host = _host()
    scenario = await Scenario(host).start()
    try:
        chat = scenario.chat
        session = host._session_for(chat)
        assert session is not None
        ref = session.content.put(b"PLAINLEAK bytes")
        await host.sequencer.publish(
            chat,
            {
                "type": "chat/turnStarted",
                "turnId": "t9",
                "startedAt": "1970-01-01T00:00:09.000Z",
                "message": {"text": "x", "origin": {"kind": "user"}},
            },
        )
        await host.sequencer.publish(
            chat,
            {
                "type": "chat/responsePart",
                "turnId": "t9",
                "part": {"kind": "markdown", "id": "p9", "content": ref["uri"]},
            },
        )
        assert session.content.owns(ref["uri"])

        await host.redact(chat, "t9", "p9", "leaked", actor="ops")

        assert not session.content.owns(ref["uri"])
        assert finished(host, chat, "t2")
        assert isinstance(state(host, chat), Mapping)
    finally:
        await scenario.close()


async def test_host_redaction_audit_is_written_even_when_the_store_fails(tmp_path: Path) -> None:
    sink = ListSink()
    store = FlakyStore(tmp_path / "sessions", debounce=0.05)
    host = _host(store=store, sequence_file=tmp_path / "seq", audit=sink)
    scenario = await Scenario(host).start()
    try:
        chat, opted = scenario.chat, scenario.opted
        part_id = scenario.part_id()
        store.failing = True
        with pytest.raises(RedactionError, match="could not be stored") as raised:
            await host.redact(chat, "t2", part_id, "leaked", actor="ops")
        assert SECRET not in str(raised.value)
        # Published (live view redacted) ...
        assert await opted.until(lambda: bool(opted.actions(chat, PART_REDACTED)))
        assert "[redacted]" in _dump(host.sequencer.state_of(chat))
        # ... and audited, with the failure on the record.
        events = [e for e in sink.events if e.kind == "chat.redacted"]
        assert len(events) == 1
        assert events[0].detail["persisted"] is False
        assert "disk full" in events[0].detail["error"]
        assert events[0].detail["actor"] == "ops"
        assert SECRET not in repr(sink.events)
        # Once the store works the next redaction writes the whole rewritten state.
        store.failing = False
        await host.redact_message(chat, "t2", "leaked", actor="ops")
        ok = [e for e in sink.events if e.kind == "chat.redacted"][-1]
        assert ok.detail["persisted"] is True
        assert ok.detail["error"] is None
        assert SECRET not in _on_disk(tmp_path / "sessions")
    finally:
        await scenario.close()


async def test_host_redaction_during_shutdown_is_an_error_and_publishes_nothing() -> None:
    sink = ListSink()
    host = _host(audit=sink)
    scenario = await Scenario(host).start()
    try:
        chat = scenario.chat
        part_id = scenario.part_id()
        await host.aclose()
        seq = host.sequencer.server_seq
        before = _dump(host.sequencer.state_of(chat))
        with pytest.raises(RedactionError, match="shutting down"):
            await host.redact(chat, "t2", part_id, "leaked", actor="ops")
        with pytest.raises(RedactionError, match="shutting down"):
            await host.redact_message(chat, "t2", "leaked", actor="ops")
        assert host.sequencer.server_seq == seq
        assert _dump(host.sequencer.state_of(chat)) == before
        assert not [e for e in sink.events if e.kind == "chat.redacted"]
    finally:
        await scenario.close()


async def test_host_redaction_target_vanishing_before_the_lock_consumes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = ListSink()
    host = _host(audit=sink)
    scenario = await Scenario(host).start()
    try:
        chat, opted = scenario.chat, scenario.opted
        # The validation outside the lock sees the part; by the time the
        # transform runs under the lock it is gone (here: it never existed).
        monkeypatch.setattr(
            "ahp_host.core.host.find_part",
            lambda state, turn, part: {"kind": "markdown", "id": part},
        )
        seq = host.sequencer.server_seq
        evicted = dict(host.sequencer._evicted_through)
        owed = dict(host.sequencer._snapshot_owed)
        seen = len(opted.actions(chat, PART_REDACTED))
        with pytest.raises(RedactionError, match="target not found"):
            await host.redact(chat, "t2", "p-vanished", "leaked", actor="ops")
        assert host.sequencer.server_seq == seq
        assert host.sequencer._evicted_through == evicted
        assert host.sequencer._snapshot_owed == owed
        assert chat in host.sequencer._log  # replay was not truncated
        await asyncio.sleep(0.05)
        assert len(opted.actions(chat, PART_REDACTED)) == seen
        assert not [e for e in sink.events if e.kind == "chat.redacted"]
        assert SECRET in _dump(state(host, chat))
    finally:
        await scenario.close()


async def test_host_redaction_needs_a_stated_actor() -> None:
    host = _host()
    scenario = await Scenario(host).start()
    try:
        chat, part_id = scenario.chat, scenario.part_id()
        with pytest.raises(TypeError):
            await host.redact(chat, "t2", part_id, "x")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            await host.redact_message(chat, "t2", "x")  # type: ignore[call-arg]
        with pytest.raises(RedactionError, match="actor"):
            await host.redact(chat, "t2", part_id, "x", actor="  ")
        assert SECRET in _dump(state(host, chat))
    finally:
        await scenario.close()
