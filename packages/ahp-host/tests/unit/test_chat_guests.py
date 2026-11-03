"""A chat on another agent runs on a guest agent session (per-chat agents, M3).

The session keeps its own agent; the first foreign chat of a provider creates that
provider's agent session (the "guest") and later chats of the provider -- forks and
side chats included -- are hosted on it. Every per-chat call goes to the chat's
agent; session-wide calls fan out or stay with the session's agent as documented.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from .guests import pair
from .hosting import connect, open_session, run_turn, shut, state, turn_started

pytestmark = pytest.mark.anyio

SESSION = "echo:/gs-1"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _chat(wire: Any, session: str, chat: str, **extra: Any) -> dict[str, Any]:
    response: dict[str, Any] = await wire.request(
        "createChat", {"channel": session, "chat": chat, **extra}
    )
    if "error" not in response:
        await wire.request("subscribe", {"channel": chat})
    return response


async def test_a_foreign_chats_turn_runs_on_the_guest_not_the_session_agent() -> None:
    host, own, guest = pair()
    wire, serving = await connect(host)
    try:
        default = await open_session(wire, SESSION)
        foreign = "ahp-chat:/gs-1b"
        assert "error" not in await _chat(wire, SESSION, foreign, provider="echo2")
        # Created with the session's working directories and URI, for that chat.
        [context] = guest.contexts
        assert context.chat_uri == foreign
        assert context.session_uri == SESSION
        assert context.provider_id == "echo2"
        await run_turn(wire, foreign, "t1", "to the guest")
        await run_turn(wire, default, "t2", "to the session")
        assert [m.text for m in guest.sessions[0].messages] == ["to the guest"]
        assert [m.text for m in own.sessions[0].messages] == ["to the session"]
        # The session's agent never heard of the foreign chat.
        assert foreign not in {c.chat_uri for c in own.sessions[0].opened}
    finally:
        await shut(host, wire, serving)


async def test_later_foreign_chats_share_the_guest_and_are_opened_on_it() -> None:
    host, _, guest = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        first, second = "ahp-chat:/gs-2a", "ahp-chat:/gs-2b"
        await _chat(wire, SESSION, first, provider="echo2")
        await _chat(wire, SESSION, second, provider="echo2")
        assert len(guest.sessions) == 1
        assert [c.chat_uri for c in guest.sessions[0].opened] == [second]
        # A fork of a foreign chat continues on the guest, as the source's agent.
        await run_turn(wire, first, "t1", "history")
        forked = "ahp-chat:/gs-2f"
        assert "error" not in await _chat(
            wire, SESSION, forked, source={"kind": "fork", "chat": first, "turnId": "t1"}
        )
        assert len(guest.sessions) == 1
        opened = guest.sessions[0].opened[-1]
        assert opened.chat_uri == forked
        assert opened.fork is not None
        assert state(host, forked)["provider"] == "echo2"
        assert host._sessions[SESSION].chat_providers[forked] == "echo2"
    finally:
        await shut(host, wire, serving)


async def test_steering_and_cancel_reach_the_chats_own_agent() -> None:
    host, own, guest = pair(delay=0.2)
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        foreign = "ahp-chat:/gs-3b"
        await _chat(wire, SESSION, foreign, provider="echo2")
        await wire.dispatch(foreign, turn_started("t1", "slow"))
        assert await wire.until(lambda: state(host, foreign).get("activeTurn") is not None)
        seq = await wire.dispatch(
            foreign,
            {
                "type": "chat/pendingMessageSet",
                "kind": "steering",
                "id": "s1",
                "message": {"text": "also this", "origin": {"kind": "user"}},
            },
        )
        echo = await wire.echoed(foreign, seq)
        assert "rejectionReason" not in echo, echo
        assert await wire.until(lambda: guest.sessions[0].steered == ["also this"])
        assert own.sessions[0].steered == []
        await wire.dispatch(foreign, {"type": "chat/turnCancelled", "turnId": "t1", "duration": 1})
        assert await wire.until(lambda: len(guest.sessions[0].cancelled) == 1)
        assert guest.sessions[0].cancelled[0][0] == foreign
        assert own.sessions[0].cancelled == []
        assert own.sessions[0].session_cancels == 0
    finally:
        await shut(host, wire, serving)


async def test_truncation_and_chat_config_reach_the_chats_own_agent() -> None:
    host, own, guest = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        foreign = "ahp-chat:/gs-4b"
        await _chat(wire, SESSION, foreign, provider="echo2")
        await run_turn(wire, foreign, "t1", "one")
        # Told the chat's values when it was created, and again when they change.
        assert guest.sessions[0].chat_config[foreign] == {"prefix": "You said:"}
        seq = await wire.dispatch(
            foreign, {"type": "chat/configChanged", "config": {"prefix": "Hi:"}}
        )
        assert "rejectionReason" not in await wire.echoed(foreign, seq)
        assert guest.sessions[0].chat_config[foreign] == {"prefix": "Hi:"}
        assert foreign not in own.sessions[0].chat_config
        seq = await wire.dispatch(foreign, {"type": "chat/truncated", "turnId": "t1"})
        assert "rejectionReason" not in await wire.echoed(foreign, seq)
        assert guest.sessions[0].truncated == [(foreign, "t1")]
        assert own.sessions[0].truncated == []
    finally:
        await shut(host, wire, serving)


async def test_a_foreign_chat_shows_the_guests_reply() -> None:
    host, _, _ = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        foreign = "ahp-chat:/gs-5b"
        await _chat(wire, SESSION, foreign, provider="echo2")
        await run_turn(wire, foreign, "t1", "hello")
        [turn] = state(host, foreign)["turns"]
        assert turn["state"] == "complete"
    finally:
        await shut(host, wire, serving)


async def test_disposing_the_last_foreign_chat_closes_the_guest() -> None:
    host, _, guest = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        a, b = "ahp-chat:/gs-6a", "ahp-chat:/gs-6b"
        await _chat(wire, SESSION, a, provider="echo2")
        await _chat(wire, SESSION, b, provider="echo2")
        session = host._sessions[SESSION]
        assert "error" not in await wire.request("disposeChat", {"channel": a})
        assert not guest.sessions[0].was_closed
        assert "echo2" in session.guests
        assert a in guest.sessions[0].closed
        assert "error" not in await wire.request("disposeChat", {"channel": b})
        assert guest.sessions[0].was_closed
        assert session.guests == {}
        assert session.chat_providers == {}
        # And a later foreign chat starts a fresh one.
        await _chat(wire, SESSION, "ahp-chat:/gs-6c", provider="echo2")
        assert len(guest.sessions) == 2
    finally:
        await shut(host, wire, serving)


async def test_disposing_the_session_and_closing_the_host_close_every_guest() -> None:
    host, own, guest = pair()
    wire, serving = await connect(host)
    await open_session(wire, SESSION)
    await _chat(wire, SESSION, "ahp-chat:/gs-7b", provider="echo2")
    assert "error" not in await wire.request("disposeSession", {"channel": SESSION})
    assert guest.sessions[0].was_closed
    assert guest.sessions[0].was_disposed
    assert own.sessions[0].was_closed
    await open_session(wire, "echo:/gs-7x")
    await _chat(wire, "echo:/gs-7x", "ahp-chat:/gs-7y", provider="echo2")
    await shut(host, wire, serving)
    assert guest.sessions[1].was_closed


async def test_session_wide_calls_fan_out_or_stay_with_the_sessions_agent() -> None:
    host, own, guest = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        await _chat(wire, SESSION, "ahp-chat:/gs-8b", provider="echo2")
        theirs, mine = guest.sessions[0], own.sessions[0]
        seq = await wire.dispatch(
            SESSION, {"type": "session/isArchivedChanged", "isArchived": True}
        )
        await wire.echoed(SESSION, seq)
        assert await wire.until(lambda: theirs.archived == [True])
        assert mine.archived == [True]
        seq = await wire.dispatch(
            SESSION, {"type": "session/workingDirectorySet", "directory": "file:///work"}
        )
        echo = await wire.echoed(SESSION, seq)
        if "rejectionReason" not in echo:
            assert await wire.until(lambda: len(theirs.directories) == 1)
            assert len(mine.directories) == 1
        # Session config goes to the session's agent only: a foreign chat with its
        # own schema does not use the session's config.
        seq = await wire.dispatch(
            SESSION, {"type": "session/configChanged", "config": {"prefix": "S:"}}
        )
        assert "rejectionReason" not in await wire.echoed(SESSION, seq)
        assert mine.config_values == [{"prefix": "S:"}]
        assert theirs.config_values == []
    finally:
        await shut(host, wire, serving)


async def test_a_guest_that_cannot_be_created_fails_the_create_with_nothing_made() -> None:
    host, _, guest = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        guest.create_error = RuntimeError("no runtime")
        response = await wire.request(
            "createChat", {"channel": SESSION, "chat": "ahp-chat:/gs-9b", "provider": "echo2"}
        )
        assert "error" in response, response
        assert not host.sequencer.has_channel("ahp-chat:/gs-9b")
        session = host._sessions[SESSION]
        assert session.guests == {}
        assert session.chat_providers == {}
        assert "ahp-chat:/gs-9b" not in session.chat_uris
    finally:
        await shut(host, wire, serving)


async def test_a_guest_that_cannot_be_told_the_chats_config_is_not_kept() -> None:
    """The fail-closed rule applies to guest agents too."""
    host, _, guest = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        # A guest that refuses the config of its first chat.
        original = guest.create_session

        async def failing(context: Any) -> Any:
            made = await original(context)
            made.fail_chat_config = True
            return made

        guest.create_session = failing  # type: ignore[method-assign]
        response = await wire.request(
            "createChat", {"channel": SESSION, "chat": "ahp-chat:/gs-10b", "provider": "echo2"}
        )
        assert "error" in response, response
        assert not host.sequencer.has_channel("ahp-chat:/gs-10b")
        session = host._sessions[SESSION]
        assert session.guests == {}
        assert guest.sessions[0].was_closed
    finally:
        await shut(host, wire, serving)


async def test_a_worker_chat_of_a_foreign_chat_belongs_to_the_guest() -> None:
    host, own, guest = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        foreign = "ahp-chat:/gs-11b"
        await _chat(wire, SESSION, foreign, provider="echo2")
        publisher = guest.sessions[0].context.publisher
        assert publisher is not None
        worker = await publisher.open_tool_chat("W", tool_call_id="c1", chat=foreign)
        assert state(host, worker.resource)["provider"] == "echo2"
        assert host._sessions[SESSION].chat_providers[worker.resource] == "echo2"
        # Its values are the parent's (told to the guest), never the session's.
        assert worker.resource in guest.sessions[0].chat_config
        assert worker.resource not in own.sessions[0].chat_config
        # The guest outlives the foreign chat while its worker is still there.
        assert "error" not in await wire.request("disposeChat", {"channel": foreign})
        assert not guest.sessions[0].was_closed
        assert "error" not in await wire.request("disposeChat", {"channel": worker.resource})
        assert guest.sessions[0].was_closed
    finally:
        await shut(host, wire, serving)


# -- moveChat (M5) ---------------------------------------------------------


async def _two_sessions(wire: Any) -> tuple[str, str]:
    await open_session(wire, SESSION)
    await open_session(wire, "echo:/gs-2")
    return SESSION, "echo:/gs-2"


async def test_a_foreign_chat_cannot_move_to_another_session() -> None:
    host, _, guest = pair()
    wire, serving = await connect(host)
    try:
        source, target = await _two_sessions(wire)
        foreign = "ahp-chat:/gs-12b"
        await _chat(wire, source, foreign, provider="echo2")
        before = state(host, source)["chats"]
        refused = await wire.request(
            "moveChat",
            {"channel": foreign, "destination": {"kind": "session", "session": target}},
        )
        # An unsupported destination is PermissionDenied here, as for a chat moving
        # to a session of another agent.
        assert refused["error"]["code"] == -32009, refused
        assert "agent" in refused["error"]["message"]
        assert state(host, source)["chats"] == before
        assert foreign in host._sessions[source].chat_uris
        assert foreign not in host._sessions[target].chat_uris
        assert host._sessions[source].chat_providers == {foreign: "echo2"}
        assert not guest.sessions[0].was_closed
        # Nor into a session of its own.
        again = await wire.request(
            "moveChat", {"channel": foreign, "destination": {"kind": "newSession"}}
        )
        assert again["error"]["code"] == -32009, again
        assert foreign in host._sessions[source].chat_uris
    finally:
        await shut(host, wire, serving)


async def test_a_tree_containing_a_foreign_chat_cannot_move() -> None:
    host, _, _ = pair()
    wire, serving = await connect(host)
    try:
        source, target = await _two_sessions(wire)
        native = "ahp-chat:/gs-13a"
        await _chat(wire, source, native)
        await run_turn(wire, native, "t1", "history")
        # A side chat is a descendant, so it moves with its parent -- but only when
        # nothing under it is foreign. Route one under it by hand.
        side = "ahp-chat:/gs-13s"
        await _chat(wire, source, side, source={"kind": "sideChat", "chat": native, "turnId": "t1"})
        host._sessions[source].chat_providers[side] = "echo2"
        refused = await wire.request(
            "moveChat",
            {"channel": native, "destination": {"kind": "session", "session": target}},
        )
        assert refused["error"]["code"] == -32009, refused
        assert native in host._sessions[source].chat_uris
        host._sessions[source].chat_providers.pop(side)
    finally:
        await shut(host, wire, serving)


async def test_same_session_reorders_of_foreign_chats_work() -> None:
    host, _, _ = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        first, second = "ahp-chat:/gs-14a", "ahp-chat:/gs-14b"
        await _chat(wire, SESSION, first, provider="echo2")
        await _chat(wire, SESSION, second, provider="echo2")
        moved = await wire.request(
            "moveChat",
            {"channel": second, "destination": {"kind": "session", "session": SESSION}},
        )
        assert moved["result"] == {"session": SESSION}, moved
        order = [c["resource"] for c in state(host, SESSION)["chats"]]
        assert order.index(second) < order.index(first)
    finally:
        await shut(host, wire, serving)


async def test_a_native_chat_still_moves_with_chat_moved_carrying_no_provider() -> None:
    host, own, _ = pair()
    wire, serving = await connect(host)
    try:
        source, target = await _two_sessions(wire)
        native = "ahp-chat:/gs-15a"
        await _chat(wire, source, native)
        await _chat(wire, source, "ahp-chat:/gs-15f", provider="echo2")
        moved = await wire.request(
            "moveChat",
            {"channel": native, "destination": {"kind": "session", "session": target}},
        )
        assert moved["result"] == {"session": target}, moved
        [action] = wire.actions(native, "chat/moved")
        assert action["session"] == target
        assert "provider" not in action
        assert "schema" not in action
        assert "provider" not in state(host, native)
    finally:
        await shut(host, wire, serving)


async def test_a_fresh_guest_never_gets_another_chats_resume_state() -> None:
    host, _, guest = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        session = host._sessions[SESSION]
        a, b = "ahp-chat:/gs-rs-a", "ahp-chat:/gs-rs-b"
        await _chat(wire, SESSION, a, provider="echo2")
        # What persisting the session while A's guest was up leaves behind.
        session.guest_resume["echo2"] = {"tag": "from-chat-a"}
        assert "error" not in await wire.request("disposeChat", {"channel": a})
        assert session.guests == {}
        assert "echo2" not in session.guest_resume
        await _chat(wire, SESSION, b, provider="echo2")
        assert [c.resume_state for c in guest.contexts] == [None, None]
    finally:
        await shut(host, wire, serving)


async def test_creating_a_foreign_chat_ignores_a_stale_resume_state() -> None:
    host, _, guest = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)
        host._sessions[SESSION].guest_resume["echo2"] = {"tag": "stale"}
        await _chat(wire, SESSION, "ahp-chat:/gs-rs-c", provider="echo2")
        [context] = guest.contexts
        assert context.resume_state is None
    finally:
        await shut(host, wire, serving)


async def test_a_rollback_does_not_close_a_guest_another_create_is_still_using() -> None:
    """Two creates on a new provider: the second reuses the guest the first made, and its
    route is not recorded until it is registered. The first failing meanwhile must not close
    the guest under the second."""
    host, _, guest = pair()
    wire, serving = await connect(host)
    wire2, serving2 = await connect(host, "c2")
    try:
        await open_session(wire, SESSION)
        await wire2.request("subscribe", {"channel": SESSION})
        a, b = "ahp-chat:/gs-race-a", "ahp-chat:/gs-race-b"
        fail_a, entered_b, gate_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = guest.create_session

        async def rigged(context: Any) -> Any:
            made = await original(context)
            config, opened = made.chat_config_changed, made.chat_opened

            async def cco(chat: str, values: Any) -> None:
                if chat == a:
                    await fail_a.wait()
                    raise RuntimeError("refused")
                await config(chat, values)

            async def open_(chat_context: Any) -> None:
                if chat_context.chat_uri == b:
                    entered_b.set()
                    await gate_b.wait()
                await opened(chat_context)

            made.chat_config_changed = cco  # type: ignore[method-assign]
            made.chat_opened = open_  # type: ignore[method-assign]
            return made

        guest.create_session = rigged  # type: ignore[method-assign]
        first = asyncio.create_task(_chat(wire, SESSION, a, provider="echo2"))
        for _ in range(200):
            if guest.sessions:
                break
            await asyncio.sleep(0.01)
        second = asyncio.create_task(_chat(wire2, SESSION, b, provider="echo2"))
        await asyncio.wait_for(entered_b.wait(), 5)
        fail_a.set()
        assert "error" in await asyncio.wait_for(first, 5)
        assert not guest.sessions[0].was_closed
        gate_b.set()
        assert "error" not in await asyncio.wait_for(second, 5)
        assert not guest.sessions[0].was_closed
        assert host._sessions[SESSION].guests == {"echo2": guest.sessions[0]}
    finally:
        fail_a.set()
        gate_b.set()
        await shut(host, wire2, serving2)
        await shut(host, wire, serving)


async def test_a_guest_whose_chats_cannot_be_attached_is_not_orphaned() -> None:
    host, _, guest = pair()
    wire, serving = await connect(host)
    try:
        await open_session(wire, SESSION)

        async def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("attach failed")

        host._attach_guest_chats = broken  # type: ignore[method-assign]
        response = await _chat(wire, SESSION, "ahp-chat:/gs-orph", provider="echo2")
        assert "error" in response, response
        session = host._sessions[SESSION]
        assert session.guests == {}
        assert guest.sessions[0].was_closed
    finally:
        await shut(host, wire, serving)
