"""What becomes of a session's open input requests across a restart.

The turn that asked is failed on restore (`host.restart`; see
`test_graceful_restart.py` for how it is resumed), and nothing in the new
process can answer what it asked. Every stored request is withdrawn: its
`inputNeeded` entry removed, and a notice naming it added to the interrupted
turn -- after a deliberate (drained) restart and after a crash alike.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import AgentSessionContext, TurnSink
from ahp_host.provider.echo import EchoSession
from tests.unit.hosting import Wire, connect, finished, open_session, shut, state, turn_started

pytestmark = pytest.mark.anyio

WITHDRAWN_CONFIRMATION = (
    "Withdrawn after a host restart, because nothing can answer them any more: "
    'approval of "Echo Tool".'
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ResumingSession(EchoSession):
    """An echo agent that can pick a failed turn back up (`ResumesTurns`)."""

    def __init__(self, context: AgentSessionContext, resumed: list[str]) -> None:
        super().__init__(context)
        self.resumed = resumed

    async def resume_turn(self, chat_uri: str, turn_id: str, sink: TurnSink) -> None:
        self.resumed.append(turn_id)
        await sink.text_delta(f"resumed {turn_id}")


class ResumingProvider(EchoProvider):
    def __init__(self) -> None:
        super().__init__()
        self.resumed: list[str] = []

    async def create_session(self, context: AgentSessionContext) -> ResumingSession:
        return ResumingSession(context, self.resumed)

    async def resume_session(self, context: AgentSessionContext) -> ResumingSession:
        return await self.create_session(context)


def _host(tmp_path: Path, provider: EchoProvider) -> Host:
    return Host(
        provider,
        LoopbackSingleUserPolicy(),
        store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
        sequence_file=tmp_path / "seq",
    )


def _turn(host: Host, chat: str, turn_id: str) -> dict[str, Any]:
    chat_state = state(host, chat)
    active = chat_state.get("activeTurn")
    candidates: list[dict[str, Any]] = [dict(active)] if isinstance(active, Mapping) else []
    candidates += chat_state.get("turns", [])
    return next(t for t in candidates if t.get("id") == turn_id)


def _parts(turn: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [p for p in turn.get("responseParts", []) if p.get("kind") == kind]


def _text(turn: Mapping[str, Any]) -> str:
    return "".join(str(p.get("content", "")) for p in _parts(turn, "markdown"))


def _notices(turn: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        p
        for p in _parts(turn, "systemNotification")
        if p.get("_meta", {}).get("ahp-py/restart", {}).get("event") == "inputWithdrawn"
    ]


async def _first_host(
    tmp_path: Path, provider: EchoProvider, uri: str, *, drain: bool
) -> list[str]:
    """Start turn 't1', wait until it is parked on a request, then stop the host.

    With *drain*, the stop is a deliberate restart whose deadline passed; without,
    it stands for a crash: the turn is active in storage and nothing says why.
    Returns the ids of the requests the first host had published.
    """
    host = _host(tmp_path, provider)
    wire, serving = await connect(host)
    chat = await open_session(wire, uri)
    await wire.dispatch(chat, turn_started("t1"))
    assert await wire.until(lambda: len(state(host, uri).get("inputNeeded", [])) == 1)
    published = [e["id"] for e in state(host, uri).get("inputNeeded", [])]
    if drain:
        assert await host.drain(0.2) == [chat]
    await shut(host, wire, serving)
    return published


async def _second_host(
    tmp_path: Path, provider: EchoProvider, uri: str, *, host: Host | None = None
) -> tuple[Host, Wire, str, asyncio.Task[None]]:
    host = host if host is not None else _host(tmp_path, provider)
    assert await host.restore() == 1
    wire, serving = await connect(host)
    await wire.request("subscribe", {"channel": uri})
    chat = state(host, uri)["defaultChat"]
    await wire.request("subscribe", {"channel": chat})
    return host, wire, chat, serving


def _confirm() -> dict[str, Any]:
    return {
        "type": "chat/toolCallConfirmed",
        "turnId": "t1",
        "toolCallId": "echo-tool-1",
        "approved": True,
        "confirmed": "user-action",
    }


async def _assert_not_wedged(host: Host, wire: Wire, uri: str, chat: str) -> None:
    """No active turn, nothing dangling, and a new turn on the chat is accepted."""
    assert await wire.until(lambda: state(host, chat).get("activeTurn") is None)
    assert state(host, chat)["turns"][-1]["id"] == "t1"
    assert state(host, uri).get("inputNeeded", []) == []
    seq = await wire.dispatch(chat, turn_started("t2", "again"))
    assert "rejectionReason" not in await wire.echoed(chat, seq)
    assert await wire.until(lambda: finished(host, chat, "t2"))


async def test_shutdown_persists_the_active_turn_and_its_open_requests(tmp_path: Path) -> None:
    host = _host(tmp_path, EchoProvider(confirm_tools=True))
    wire, serving = await connect(host)
    session = "echo:/m6-a"
    chat = await open_session(wire, session)
    await wire.dispatch(chat, turn_started("t1"))
    assert await wire.until(lambda: len(state(host, session).get("inputNeeded", [])) == 1)
    rid = state(host, session)["inputNeeded"][0]["id"]
    await shut(host, wire, serving)

    stored = await FileSessionStore(tmp_path / "sessions").load_all()
    assert len(stored) == 1
    assert stored[0].input_requests == [
        {"id": rid, "kind": "confirm", "chat": chat, "turnId": "t1", "key": "echo-tool-1"}
    ]
    assert stored[0].channels[chat]["activeTurn"]["id"] == "t1"
    assert [e["id"] for e in stored[0].channels[session]["inputNeeded"]] == [rid]


async def test_restart_recovery_a_crash_withdraws_a_confirmation_with_a_notice(
    tmp_path: Path,
) -> None:
    uri = "echo:/m6-crash"
    (first_id,) = await _first_host(tmp_path, EchoProvider(confirm_tools=True), uri, drain=False)
    provider = ResumingProvider()
    host, wire, chat, serving = await _second_host(tmp_path, provider, uri)
    try:
        assert state(host, uri).get("inputNeeded", []) == []
        last = _turn(host, chat, "t1")
        assert last["state"] == "error"
        notices = _notices(last)
        assert [n["content"] for n in notices] == [WITHDRAWN_CONFIRMATION]
        assert notices[0]["_meta"]["ahp-py/restart"]["requests"] == [first_id]
        # The notice comes just before the error the restart failed the turn with.
        assert last["responseParts"][-2] == notices[0]
        assert last["responseParts"][-1]["error"]["errorType"] == "host.restart"
        # A crash resumes nothing by itself, and an answer to the old id finds nothing.
        await asyncio.sleep(0.2)
        assert provider.resumed == []
        seq = await wire.dispatch(chat, _confirm())
        assert "rejectionReason" in await wire.echoed(chat, seq)
        await _assert_not_wedged(host, wire, uri, chat)
    finally:
        await shut(host, wire, serving)


async def test_restart_recovery_a_drain_restart_withdraws_and_resumes_once(
    tmp_path: Path,
) -> None:
    uri = "echo:/m6-drain"
    await _first_host(tmp_path, EchoProvider(confirm_tools=True), uri, drain=True)
    provider = ResumingProvider()
    host, wire, chat, serving = await _second_host(tmp_path, provider, uri)
    try:
        assert await wire.until(lambda: finished(host, chat, "t1"))
        assert provider.resumed == ["t1"]
        assert state(host, uri).get("inputNeeded", []) == []
        last = _turn(host, chat, "t1")
        assert last["state"] == "complete"
        # The resumed turn keeps the notice, then what the agent added.
        assert [n["content"] for n in _notices(last)] == [WITHDRAWN_CONFIRMATION]
        assert "resumed t1" in _text(last)
    finally:
        await shut(host, wire, serving)


async def test_restart_recovery_an_input_request_is_withdrawn_with_a_notice(
    tmp_path: Path,
) -> None:
    uri = "echo:/m6-input"
    (first_id,) = await _first_host(tmp_path, EchoProvider(elicit=True), uri, drain=False)
    host, wire, chat, serving = await _second_host(tmp_path, EchoProvider(), uri)
    try:
        assert state(host, uri).get("inputNeeded", []) == []
        last = _turn(host, chat, "t1")
        (notice,) = _notices(last)
        assert notice["_meta"]["ahp-py/restart"]["requests"] == [first_id]
        assert str(notice["content"]).startswith("Withdrawn after a host restart")
        assert "question" in str(notice["content"])
        # The unanswered question itself is gone from the failed turn.
        assert _parts(last, "inputRequest") == []
        await _assert_not_wedged(host, wire, uri, chat)
    finally:
        await shut(host, wire, serving)


async def test_restart_recovery_an_old_store_file_still_loads(tmp_path: Path) -> None:
    """A file written before requests were stored: the entry alone is withdrawn the same way."""
    uri = "echo:/m6-old"
    await _first_host(tmp_path, EchoProvider(confirm_tools=True), uri, drain=False)
    (path,) = list((tmp_path / "sessions").glob("*.json"))
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["inputRequests"]
    path.write_text(json.dumps(document), encoding="utf-8")
    host, wire, chat, serving = await _second_host(tmp_path, EchoProvider(), uri)
    try:
        assert state(host, uri).get("inputNeeded", []) == []
        assert [n["content"] for n in _notices(_turn(host, chat, "t1"))] == [WITHDRAWN_CONFIRMATION]
        await _assert_not_wedged(host, wire, uri, chat)
    finally:
        await shut(host, wire, serving)


async def test_restart_recovery_a_stale_request_is_withdrawn(tmp_path: Path) -> None:
    """An entry whose turn is no longer active has no turn to hold a notice: still removed."""
    uri = "echo:/m6-stale"
    await _first_host(tmp_path, EchoProvider(confirm_tools=True), uri, drain=False)
    (path,) = list((tmp_path / "sessions").glob("*.json"))
    document = json.loads(path.read_text(encoding="utf-8"))
    for channel in document["channels"].values():
        channel.pop("activeTurn", None)
    path.write_text(json.dumps(document), encoding="utf-8")
    host, wire, chat, serving = await _second_host(tmp_path, EchoProvider(), uri)
    try:
        assert state(host, uri).get("inputNeeded", []) == []
        assert state(host, chat).get("activeTurn") is None
        seq = await wire.dispatch(chat, turn_started("t2", "again"))
        assert "rejectionReason" not in await wire.echoed(chat, seq)
        assert await wire.until(lambda: finished(host, chat, "t2"))
    finally:
        await shut(host, wire, serving)


async def test_restart_recovery_a_crash_with_an_active_turn_never_wedges_the_chat(
    tmp_path: Path,
) -> None:
    """A store holding `activeTurn` (what a crash leaves) is failed, not left active."""
    uri = "echo:/m6-wedge"
    await _first_host(tmp_path, EchoProvider(confirm_tools=True), uri, drain=False)
    stored = await FileSessionStore(tmp_path / "sessions").load_all()
    assert any("activeTurn" in c for c in stored[0].channels.values())
    host, wire, chat, serving = await _second_host(tmp_path, EchoProvider(), uri)
    try:
        last = _turn(host, chat, "t1")
        assert [p["error"]["errorType"] for p in _parts(last, "error")] == ["host.restart"]
        await _assert_not_wedged(host, wire, uri, chat)
    finally:
        await shut(host, wire, serving)


async def test_restart_recovery_a_resume_that_cannot_start_fails_the_turn_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reducer has reopened the turn when starting it fails: it must not stay active."""
    uri = "echo:/m6-start-fails"
    await _first_host(tmp_path, EchoProvider(confirm_tools=True), uri, drain=True)
    provider = ResumingProvider()
    host = _host(tmp_path, provider)
    original = host._start_turn

    async def failing(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("resume"):
            raise RuntimeError("cannot start")
        await original(*args, **kwargs)

    monkeypatch.setattr(host, "_start_turn", failing)
    host, wire, chat, serving = await _second_host(tmp_path, provider, uri, host=host)
    try:
        last = _turn(host, chat, "t1")
        errors = _parts(last, "error")
        assert errors[-1]["error"]["errorType"] == "host.restart"
        assert errors[-1]["resumable"] is True
        assert provider.resumed == []
        await _assert_not_wedged(host, wire, uri, chat)
    finally:
        await shut(host, wire, serving)
