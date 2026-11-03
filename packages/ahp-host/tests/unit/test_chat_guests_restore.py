"""Persisting and restoring foreign chats (per-chat agents, M4).

`chat_providers` is rebuilt from each chat's `ChatState.provider` -- the same fact,
already stored with the chat -- and each guest's `resume_state_of` is saved beside the
session's. A guest is resumed lazily, on first use of one of its chats.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.store import FileSessionStore, StoredSession

from .guests import RecordingProvider, pair
from .hosting import connect, open_session, run_turn, shut, state

pytestmark = pytest.mark.anyio

SESSION = "echo:/gr-1"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _foreign(wire: Any, session: str, chat: str) -> None:
    response = await wire.request(
        "createChat", {"channel": session, "chat": chat, "provider": "echo2"}
    )
    assert "error" not in response, response
    await wire.request("subscribe", {"channel": chat})


async def _first_run(tmp_path: Path) -> tuple[str, str]:
    host, _, _ = pair(store=FileSessionStore(tmp_path))
    wire, serving = await connect(host)
    default = await open_session(wire, SESSION)
    foreign = "ahp-chat:/gr-1b"
    await _foreign(wire, SESSION, foreign)
    await run_turn(wire, foreign, "t1", "before")
    await shut(host, wire, serving)
    return default, foreign


def test_guest_resume_states_round_trip_and_default_to_empty() -> None:
    stored = StoredSession(uri="echo:/s", provider="echo", created_at="t", channels={})
    assert stored.guest_resume_states == {}
    assert "guestResumeStates" not in stored.to_json()
    saved = StoredSession(
        uri="echo:/s",
        provider="echo",
        created_at="t",
        channels={},
        guest_resume_states={"echo2": {"k": 1}},
    )
    assert StoredSession.from_json(saved.to_json()).guest_resume_states == {"echo2": {"k": 1}}
    with pytest.raises(ValueError, match="guestResumeStates"):
        StoredSession.from_json({**saved.to_json(), "guestResumeStates": {"echo2": 3}})


async def test_a_restored_foreign_chat_resumes_its_guest_on_first_use(tmp_path: Path) -> None:
    default, foreign = await _first_run(tmp_path)
    [stored] = list(await FileSessionStore(tmp_path).load_all())
    assert stored.guest_resume_states == {"echo2": {"tag": "echo2-state"}}

    host, own, guest = pair(store=FileSessionStore(tmp_path))
    assert await host.restore() == 1
    wire, serving = await connect(host)
    try:
        await wire.request("subscribe", {"channel": SESSION})
        await wire.request("subscribe", {"channel": foreign})
        session = host._sessions[SESSION]
        assert session.chat_providers == {foreign: "echo2"}
        # Lazy: nothing is running yet, on either side.
        assert guest.resumed == []
        assert own.resumed == []
        await run_turn(wire, foreign, "t2", "after")
        [context] = guest.resumed
        assert context.chat_uri == foreign
        assert context.resume_state == {"tag": "echo2-state"}
        assert [m.text for m in guest.sessions[0].messages] == ["after"]
        # Only the guest was needed: the session's own agent is still down.
        assert own.resumed == []
        assert session.agent_session is None
        assert state(host, foreign)["provider"] == "echo2"
    finally:
        await shut(host, wire, serving)


async def test_a_new_foreign_chat_on_a_restored_provider_resumes_instead_of_creating(
    tmp_path: Path,
) -> None:
    _, foreign = await _first_run(tmp_path)
    host, _, guest = pair(store=FileSessionStore(tmp_path))
    assert await host.restore() == 1
    wire, serving = await connect(host)
    try:
        await wire.request("subscribe", {"channel": SESSION})
        await _foreign(wire, SESSION, "ahp-chat:/gr-1c")
        assert len(guest.resumed) == 1
        assert guest.contexts == []
        # The restored chat is announced on it as restored; the new one as new.
        opened = {c.chat_uri: c.restored for c in guest.sessions[0].opened}
        assert opened == {"ahp-chat:/gr-1c": False}
        assert guest.sessions[0].context.chat_uri == foreign
    finally:
        await shut(host, wire, serving)


async def test_a_provider_that_is_gone_leaves_the_chat_present_but_refuses_its_turns(
    tmp_path: Path,
) -> None:
    default, foreign = await _first_run(tmp_path)
    only = RecordingProvider("echo")
    host = Host(only, LoopbackSingleUserPolicy(), store=FileSessionStore(tmp_path))
    assert await host.restore() == 1
    wire, serving = await connect(host)
    try:
        await wire.request("subscribe", {"channel": SESSION})
        await wire.request("subscribe", {"channel": foreign})
        assert foreign in host._sessions[SESSION].chat_uris
        assert state(host, foreign)["provider"] == "echo2"
        await run_turn(wire, foreign, "t2", "anyone there")
        [*_, turn] = state(host, foreign)["turns"]
        assert turn["state"] == "error"
        [part] = turn["responseParts"]
        assert "echo2" in part["error"]["message"]
        # And the native chat is unaffected.
        await run_turn(wire, default, "t3", "native")
        [*_, native] = state(host, default)["turns"]
        assert native["state"] == "complete"
    finally:
        await shut(host, wire, serving)


async def test_a_host_without_the_opt_in_still_serves_existing_foreign_chats(
    tmp_path: Path,
) -> None:
    _, foreign = await _first_run(tmp_path)
    host, _, guest = pair(store=FileSessionStore(tmp_path), multiple_chat_providers=False)
    assert await host.restore() == 1
    wire, serving = await connect(host)
    try:
        assert "multipleChatProviders" not in wire.initialized
        await wire.request("subscribe", {"channel": SESSION})
        await wire.request("subscribe", {"channel": foreign})
        await run_turn(wire, foreign, "t2", "still served")
        assert [m.text for m in guest.sessions[0].messages] == ["still served"]
        # ...but new ones are refused.
        refused = await wire.request(
            "createChat", {"channel": SESSION, "chat": "ahp-chat:/gr-1x", "provider": "echo2"}
        )
        assert refused["error"]["code"] == -32602
    finally:
        await shut(host, wire, serving)
