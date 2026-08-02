"""The front door, driven end to end against a programmable host."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_host_client.api import (
    Delta,
    ToolCallReady,
    TurnCompleted,
    approve_all,
    auto,
    connect,
    deny_all,
    resolve_policy,
)
from agent_host_client.client.errors import AhpClientError
from agent_host_client.testing import FakeHost, FakeToolCall, echo_host

SESSION_STATE = {
    "lifecycle": "ready",
    "defaultChat": "ahp-chat://c/s",
    "interactivity": "full",
}


def _session_host(**overrides: Any) -> FakeHost:
    host = echo_host()
    state = {**SESSION_STATE, **overrides}

    def subscribe(params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        if channel.startswith("ahp-chat:"):
            body: Any = {"turns": [], "activeTurn": None}
        elif channel == "ahp-root://":
            body = host.root_state
        else:
            body = state
        # `fromSeq` is the last number ISSUED. Reporting the *next* one makes
        # the first action look already-included, and a client that honours the
        # ordering rule correctly then drops it -- which is what this helper did
        # on its first draft, and what the mirror caught.
        return {"snapshot": {"resource": channel, "state": body, "fromSeq": host._server_seq}}

    host.on("subscribe", subscribe)
    host.on("createSession", lambda _p: {})
    host.on("disposeSession", lambda _p: {})
    return host


# ── approvals ────────────────────────────────────────────────────────────────


async def test_manual_is_the_default_and_does_not_silently_approve() -> None:
    """Silently approving is the obvious wrong default; silently approving *is*
    what a permissive default would do."""
    policy = resolve_policy(None)
    call = ToolCallReady({"action": {"toolName": "rm"}})
    assert await policy(call) is False


async def test_string_shorthands_resolve() -> None:
    call = ToolCallReady({"action": {"toolName": "read"}})
    assert await resolve_policy("all")(call) is True
    assert await resolve_policy("none")(call) is False
    with pytest.raises(ValueError, match="unknown approval policy"):
        resolve_policy("sometimes")


async def test_reads_really_inspects_the_read_only_hint() -> None:
    """`ahpx`'s equivalent silently degrades to prompting for everything, and
    its own docs say otherwise."""
    policy = auto(read_only=True)
    readable = ToolCallReady({"action": {"toolName": "cat", "annotations": {"readOnlyHint": True}}})
    writable = ToolCallReady({"action": {"toolName": "rm", "annotations": {"readOnlyHint": False}}})
    unannotated = ToolCallReady({"action": {"toolName": "mystery"}})
    assert await policy(readable) is True
    assert await policy(writable) is False
    assert await policy(unannotated) is False  # falls back, does not guess


async def test_allow_and_deny_lists_win_over_the_hint() -> None:
    policy = auto(allow=["safe"], deny=["dangerous"], read_only=True)
    dangerous = ToolCallReady(
        {"action": {"toolName": "dangerous", "annotations": {"readOnlyHint": True}}}
    )
    assert await policy(dangerous) is False
    assert await policy(ToolCallReady({"action": {"toolName": "safe"}})) is True


# ── connect ──────────────────────────────────────────────────────────────────


async def test_connect_requires_exactly_one_target() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        connect()

    async def factory() -> Any:  # pragma: no cover - never called
        raise AssertionError("unreachable")

    with pytest.raises(ValueError, match="exactly one"):
        connect("ws://x", transport_factory=factory)


async def test_connect_over_a_supplied_transport() -> None:
    host = echo_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        assert client.protocol_version == "0.7.0"
        assert client.client_id
        assert client.root["agents"] == [{"provider": "echo", "displayName": "Echo"}]
    await host.stop()


async def test_connect_is_awaitable_as_well_as_a_context_manager() -> None:
    host = echo_host()
    await host.start()
    client = await connect(transport=host.transport())
    assert client.protocol_version == "0.7.0"
    await client.aclose()
    await host.stop()


async def test_the_raw_protocol_is_always_reachable() -> None:
    """`client.protocol.request(...)` must work for a method we have never heard
    of, or this library becomes a ceiling."""
    host = echo_host()
    await host.start()
    host.on("someFutureMethod", lambda _p: {"ok": True})
    async with connect(transport=host.transport()) as client:
        assert await client.protocol.request("someFutureMethod", {}) == {"ok": True}
    await host.stop()


# ── sessions ─────────────────────────────────────────────────────────────────


async def test_create_session_mints_a_provider_scoped_uri() -> None:
    """`<provider>:/<uuid>`, matching VS Code -- not `ahp-session:`."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo", cwd=".")
        assert session.uri.startswith("echo:/")
        created = next(m for m in host.received if m.get("method") == "createSession")
        assert created["params"]["channel"] == session.uri
        # Plural since 0.7.0.
        assert "workingDirectories" in created["params"]
    await host.stop()


async def test_a_provisional_session_returns_immediately() -> None:
    """Waiting unconditionally deadlocks for the full timeout against a host
    that answers `lifecycle: "creating"`."""
    host = _session_host(lifecycle="creating")
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await asyncio.wait_for(
            client.create_session(provider="echo", ready_timeout=30.0), 2
        )
        assert session.state["lifecycle"] == "creating"
    await host.stop()


async def test_progress_mints_a_token_because_otherwise_nothing_fires() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        await client.create_session(provider="echo", progress=True)
        created = next(m for m in host.received if m.get("method") == "createSession")
        assert created["params"]["progressToken"]
    await host.stop()


async def test_a_session_we_did_not_create_is_never_disposed_on_exit() -> None:
    """Disposing someone else's session on a `with` exit is the kind of surprise
    that loses trust."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        async with await client.open_session("echo:/theirs"):
            pass
        assert not [m for m in host.received if m.get("method") == "disposeSession"]
    await host.stop()


async def test_a_read_only_chat_refuses_to_send() -> None:
    """`ChatState.interactivity` is undocumented in every guide and gates
    whether a client may send at all."""
    host = _session_host(interactivity="read-only")
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        with pytest.raises(AhpClientError, match="read-only"):
            await session.prompt("hi")
    await host.stop()


# ── turns ────────────────────────────────────────────────────────────────────


async def test_a_turn_streams_deltas_and_ends_with_authoritative_text() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        stream = chat.prompt("hello", turn_id="t1")
        seen: list[str] = []

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(chat.uri, "t1", text="BANANA")

        driver = asyncio.get_running_loop().create_task(drive())
        async for event in stream:
            if isinstance(event, Delta):
                seen.append(event.text)
        await driver
        assert "".join(seen) == "BANANA"
    await host.stop()


async def test_a_folded_first_delta_is_recovered_from_the_mirror() -> None:
    """Hosts fold a turn's opening characters into the response part instead of
    emitting them, so a consumer that renders only deltas shows "ANANA"."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        stream = chat.prompt("hello", turn_id="t1")

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(chat.uri, "t1", text="BANANA", fold_first_delta=True)

        driver = asyncio.get_running_loop().create_task(drive())
        deltas: list[str] = []
        async for event in stream:
            if isinstance(event, Delta):
                deltas.append(event.text)
        await driver
        # The deltas alone are missing the opening character...
        assert "".join(deltas) == "ANANA"
        # ...and the authoritative text is not.
        assert stream.text() == "BANANA"
    await host.stop()


async def test_awaiting_a_turn_drains_it_and_returns_the_final_event() -> None:
    """One object, two idioms, one code path."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(chat.uri, "t1", text="done")

        driver = asyncio.get_running_loop().create_task(drive())
        result = await chat.prompt("hello", turn_id="t1", approvals="all")
        await driver
        assert isinstance(result, TurnCompleted)
        assert result.text == "done"
    await host.stop()


async def test_a_tool_call_can_be_approved_from_the_event_itself() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(
                chat.uri,
                "t1",
                text="ok",
                tools=[FakeToolCall("tc1", "read_file", confirmed=True)],
            )

        driver = asyncio.get_running_loop().create_task(drive())
        approved = False
        async for event in chat.prompt("hello", turn_id="t1"):
            if isinstance(event, ToolCallReady):
                event.approve()
                approved = True
        await driver
        assert approved
        confirmations = [
            m
            for m in host.received
            if m.get("method") == "dispatchAction"
            and m["params"]["action"]["type"] == "chat/toolCallConfirmed"
        ]
        assert confirmations[-1]["params"]["action"]["approved"] is True
    await host.stop()


async def test_an_event_without_a_dispatcher_says_so_instead_of_failing_quietly() -> None:
    call = ToolCallReady({"action": {"toolCallId": "x"}})
    with pytest.raises(RuntimeError, match="dispatcher"):
        call.approve()


async def test_events_from_another_turn_are_filtered_out() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        async def drive() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(chat.uri, "other", text="NOISE")
            await host.emit_turn(chat.uri, "t1", text="MINE")

        driver = asyncio.get_running_loop().create_task(drive())
        deltas: list[str] = []
        async for event in chat.prompt("hello", turn_id="t1"):
            if isinstance(event, Delta):
                deltas.append(event.text)
        await driver
        assert "".join(deltas) == "MINE"
    await host.stop()


async def test_an_idle_turn_times_out_with_a_specific_message() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        stream = chat.prompt("hello", turn_id="t1", idle_timeout=0.05)
        with pytest.raises(TimeoutError, match="t1"):
            async for _event in stream:
                pass
    await host.stop()


def test_policies_are_plain_callables_so_a_caller_can_write_their_own() -> None:
    assert callable(approve_all())
    assert callable(deny_all())
