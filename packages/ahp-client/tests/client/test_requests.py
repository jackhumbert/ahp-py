"""The embedder requests in `docs/requests.md`, items 1-4.

Written from the shape the requests describe: a second client, holding a
connection against a host it did not start, attached to a session somebody else
created, rendering a turn it did not begin.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import pytest

from agent_host_client import ChatWatch, TurnInProgress, event_for
from agent_host_client.api import Delta, TurnCompleted, TurnStarted, connect
from agent_host_client.client.errors import RpcError, TransportError
from agent_host_client.hosts.policy import (
    ReconnectPolicy,
    default_should_retry,
    exponential_policy,
    retry_everything,
)
from agent_host_client.testing import FakeHost, echo_host

CHAT = "ahp-chat://c/s"


def _session_host(**overrides: Any) -> FakeHost:
    host = echo_host()
    state = {"lifecycle": "ready", "defaultChat": CHAT, "interactivity": "full", **overrides}

    def subscribe(params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        if channel.startswith("ahp-chat:"):
            body: Any = {"turns": [], "activeTurn": None}
        elif channel == "ahp-root://":
            body = host.root_state
        else:
            body = state
        return {"snapshot": {"resource": channel, "state": body, "fromSeq": host._server_seq}}

    host.on("subscribe", subscribe)
    host.on("createSession", lambda _p: {})
    host.on("disposeSession", lambda _p: {})
    return host


# ── item 1: watching a turn we did not start ─────────────────────────────────


async def test_watch_sees_a_turn_started_by_someone_else() -> None:
    """The thing the protocol exists for, and the thing TurnStream cannot do:
    it dispatches on entry and filters on a turn id of its own making."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.open_session("echo:/theirs")
        # No `defaultChat` on an opened session's snapshot here, so watch the
        # chat directly -- which is the shape a second client is in anyway.
        await client._runtime.subscribe(CHAT, "chat")
        from agent_host_client.api.client import Chat

        watch = Chat(client, session, CHAT).watch()

        async def other_client_starts_a_turn() -> None:
            await asyncio.sleep(0.05)
            await host.emit_turn(CHAT, "not-ours", text="hello")

        driver = asyncio.get_running_loop().create_task(other_client_starts_a_turn())
        seen: list[str] = []
        started = False
        async for event in watch:
            if isinstance(event, TurnStarted):
                started = True
            if isinstance(event, Delta):
                seen.append(event.text)
            if isinstance(event, TurnCompleted):
                break
        await driver
        assert started, "TurnStarted for another client's turn never arrived"
        assert "".join(seen) == "hello"
    await host.stop()


async def test_watch_dispatches_nothing_on_entry() -> None:
    """A read-only surface must not write. `TurnStream._start` sends
    chat/turnStarted before the first read; this must not."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        watch = chat.watch()
        watch._open()
        await asyncio.sleep(0.02)
        assert not [m for m in host.received if m.get("method") == "dispatchAction"]
        await watch.aclose()
    await host.stop()


async def test_attaching_mid_turn_yields_a_synthetic_turn_in_progress() -> None:
    """A UI needs something to open a bubble on. Typed distinctly, because a
    forged TurnStarted is indistinguishable from a turn that really began now."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()

        # A turn is running before anyone attaches.
        await host.push(CHAT, {"type": "chat/turnStarted", "turnId": "live"})
        await host.push(
            CHAT,
            {
                "type": "chat/responsePart",
                "turnId": "live",
                "part": {"id": "p0", "kind": "markdown", "content": "already "},
            },
        )
        await host.push(
            CHAT, {"type": "chat/delta", "turnId": "live", "partId": "p0", "content": "going"}
        )
        await asyncio.sleep(0.05)

        watch = chat.watch()
        first = await asyncio.wait_for(watch.__anext__(), 1)
        assert isinstance(first, TurnInProgress)
        assert first.turn_id == "live"
        assert first.text == "already going"
        await watch.aclose()
    await host.stop()


async def test_from_start_false_skips_the_synthetic_event() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        chat = await session.chat()
        await host.push(CHAT, {"type": "chat/turnStarted", "turnId": "live"})
        await asyncio.sleep(0.05)

        watch = chat.watch(from_start=False)

        async def later() -> None:
            await asyncio.sleep(0.05)
            await host.push(CHAT, {"type": "chat/turnComplete", "turnId": "live"})

        driver = asyncio.get_running_loop().create_task(later())
        first = await asyncio.wait_for(watch.__anext__(), 1)
        assert isinstance(first, TurnCompleted)
        await driver
        await watch.aclose()
    await host.stop()


async def test_session_watch_folds_in_the_default_chat() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        watch = await session.watch()
        assert isinstance(watch, ChatWatch)
        await watch.aclose()
    await host.stop()


def test_event_for_is_public() -> None:
    """An embedder writing its own loop otherwise imports a private name or
    writes a second _BY_TYPE -- and the second copy is the one that will not
    know about the next action type added here."""
    event = event_for({"channel": CHAT, "action": {"type": "chat/delta", "content": "x"}})
    assert isinstance(event, Delta)
    assert event.text == "x"


# ── item 2: answering as a non-originating client ────────────────────────────


async def test_pending_inputs_and_responder_are_on_session() -> None:
    """The capability existed under `serve/`; an embedder reading `api/`
    concluded it was unsupported, because at that altitude it was."""
    host = _session_host(
        inputNeeded=[
            {
                "kind": "toolConfirmation",
                "id": f"{CHAT}#tc1",
                "chat": CHAT,
                "turnId": "turn-1",
                "toolCall": {"toolCallId": "tc1"},
            }
        ]
    )
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        entries = session.pending_inputs()
        assert len(entries) == 1

        # The entry goes in whole. It is specified to carry "every identifier
        # needed to construct the response", and the caller picking fields out
        # of it by hand is how `turnId` went missing in the first place.
        session.responder.confirm_tool(entries[0], approved=True)
        await asyncio.sleep(0.02)
        action = [m for m in host.received if m.get("method") == "dispatchAction"][-1]["params"]
        assert action["channel"] == CHAT
        assert action["action"]["type"] == "chat/toolCallConfirmed"
        assert action["action"]["turnId"] == "turn-1"
        assert action["action"]["toolCallId"] == "tc1"
        assert action["action"]["approved"] is True
    await host.stop()


async def test_the_responder_is_the_same_object_across_calls() -> None:
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        assert session.responder is session.responder
    await host.stop()


async def test_inputs_yields_when_the_pending_set_changes() -> None:
    """So a session list can render "waiting on a human" without polling state
    or hand-rolling envelope filters."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")

        async def raise_a_request() -> None:
            await asyncio.sleep(0.05)
            # `session/inputNeededSet`, not an invented name -- an action type
            # the reducer does not know returns state unchanged, silently, and
            # a poll loop waiting on it never terminates. The first draft of
            # this test hung for exactly that reason.
            await host.push(
                session.uri,
                {
                    "type": "session/inputNeededSet",
                    "request": {
                        "kind": "toolConfirmation",
                        "id": f"{CHAT}#tc1",
                        "chat": CHAT,
                        "toolCall": {"toolCallId": "tc1", "status": "pending-confirmation"},
                    },
                },
            )

        driver = asyncio.get_running_loop().create_task(raise_a_request())
        seen: list[list[Any]] = []

        async def collect() -> None:
            async for pending in session.inputs(poll=0.01):
                seen.append(pending)
                if pending:
                    return

        await asyncio.wait_for(collect(), 5)
        await driver
        assert seen[0] == []
        assert seen[-1][0]["kind"] == "toolConfirmation"
    await host.stop()


async def test_inputs_wakes_on_the_envelope_not_on_the_interval() -> None:
    """Item 9. The proof that the clock is the envelope: *poll* is set far
    beyond the test's own patience, so anything the interval could explain has
    timed out long before. A 30 s ceiling that still answers in milliseconds is
    only possible if the wake came from the session channel."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")

        async def raise_a_request() -> None:
            await asyncio.sleep(0.05)
            await host.push(
                session.uri,
                {
                    "type": "session/inputNeededSet",
                    "request": {
                        "kind": "toolConfirmation",
                        "id": f"{CHAT}#tc9",
                        "chat": CHAT,
                        "toolCall": {"toolCallId": "tc9", "status": "pending-confirmation"},
                    },
                },
            )

        driver = asyncio.get_running_loop().create_task(raise_a_request())
        seen: list[list[Any]] = []

        async def collect() -> None:
            async for pending in session.inputs(poll=30.0):
                seen.append(pending)
                if pending:
                    return

        await asyncio.wait_for(collect(), 2)
        await driver
        assert seen[0] == []
        assert seen[-1][0]["toolCall"]["toolCallId"] == "tc9"
    await host.stop()


async def test_the_input_clock_ignores_other_channels() -> None:
    """Tested on the wait directly, and deliberately so.

    Through `inputs()` this is invisible: an unfiltered wake re-reads the mirror,
    finds the set unchanged and yields nothing, so a black-box test passes either
    way and proves nothing. The filter is an efficiency property -- a busy chat
    must not cost a mirror read and a list comparison per delta -- and the honest
    place to assert an efficiency property is where it lives.
    """
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        await session.chat()  # so the chat push below really does reach the reader
        reader = client._runtime.events()

        await host.push(CHAT, {"type": "chat/turnStarted", "turnId": "t1", "message": {}})
        await asyncio.sleep(0.05)
        waiting = asyncio.get_running_loop().create_task(
            session._wait_for_input_change(reader, 30.0)
        )
        await asyncio.sleep(0.05)
        assert not waiting.done(), "a chat envelope woke the session's input clock"

        await host.push(
            session.uri,
            {
                "type": "session/inputNeededSet",
                "request": {
                    "kind": "toolConfirmation",
                    "id": f"{CHAT}#tc2",
                    "chat": CHAT,
                    "toolCall": {"toolCallId": "tc2", "status": "pending-confirmation"},
                },
            },
        )
        assert await asyncio.wait_for(waiting, 2) is True
        await reader.aclose()
    await host.stop()


async def test_the_input_clock_falls_back_to_the_ceiling() -> None:
    """The ceiling is a backstop for anything that moves `inputNeeded` without
    an event scoped here -- a resubscribe snapshot after a reconnect. Nothing is
    pushed, so only the ceiling can end this wait."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        reader = client._runtime.events()
        assert await asyncio.wait_for(session._wait_for_input_change(reader, 0.05), 2) is True
        await reader.aclose()
    await host.stop()


async def test_inputs_ends_when_the_event_stream_does() -> None:
    """A generator that outlives its reader would hang on the ceiling forever
    instead of ending."""
    host = _session_host()
    await host.start()
    async with connect(transport=host.transport()) as client:
        session = await client.create_session(provider="echo")
        stream = session.inputs(poll=30.0)
        assert await stream.__anext__() == []

        drained = asyncio.get_running_loop().create_task(
            _collect_remaining(stream)  # ends only when the stream does
        )
        await asyncio.sleep(0.05)
        await client.aclose()
        assert await asyncio.wait_for(drained, 2) == []
    await host.stop()


async def _collect_remaining(stream: Any) -> list[Any]:
    return [item async for item in stream]


# ── item 3: a rejected credential is not retried ─────────────────────────────


def test_a_refused_upgrade_is_not_retried() -> None:
    """An identity-aware proxy is the standard deployment shape off loopback,
    and per-user tokens expire. Retrying is load against the proxy already
    saying no, while the user sees "reconnecting" forever."""
    assert not default_should_retry(TransportError("rejected", "401", status=401))
    assert not default_should_retry(TransportError("rejected", "403", status=403))


def test_a_policy_violation_close_is_not_retried() -> None:
    assert not default_should_retry(TransportError("rejected", "1008", close_code=1008))


def test_a_version_disagreement_is_not_retried() -> None:
    """No amount of waiting introduces a version both ends speak."""
    assert not default_should_retry(RpcError(-32005, "no mutually supported version"))


@pytest.mark.parametrize(
    "failure",
    [
        TransportError("io", "connection reset"),
        TransportError("closed", "abnormal close", close_code=1006),
        TransportError("rejected", "500", status=500),
        RpcError(-32001, "session not found"),
        OSError("connection refused"),
    ],
)
def test_everything_else_stays_transient(failure: BaseException) -> None:
    """ "The host was restarting" is far more common than any permanent refusal,
    and guessing wrong in that direction costs a connection that would have come
    back."""
    assert default_should_retry(failure)


def test_retry_everything_restores_the_older_behaviour() -> None:
    assert retry_everything(TransportError("rejected", "401", status=401))


def test_the_default_policy_carries_the_classifier() -> None:
    assert exponential_policy().should_retry is default_should_retry


async def test_a_rejected_connection_reaches_failed_without_burning_attempts() -> None:
    """`failed` is broadcast on state_changes(), which is what an embedder maps
    onto a re-login prompt. A finite attempt budget is not a substitute: it
    still burns every attempt and ends in the same place."""
    attempts = 0

    async def refusing_factory() -> Any:
        nonlocal attempts
        attempts += 1
        raise TransportError("rejected", "refused with HTTP 401", status=401)

    from agent_host_client.hosts import HostConfig, HostRuntime

    runtime = HostRuntime(
        HostConfig(refusing_factory, label="h", reconnect_policy=ReconnectPolicy())
    )
    await runtime.start(wait=False)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if runtime.state.status == "failed":
            break
    assert runtime.state.status == "failed"
    assert attempts == 1, "a permanent refusal was retried"
    assert isinstance(runtime.state.error, TransportError)
    assert runtime.state.error.status == 401
    await runtime.shutdown()


async def test_a_transient_failure_still_retries() -> None:
    attempts = 0

    async def flaky_factory() -> Any:
        nonlocal attempts
        attempts += 1
        raise TransportError("io", "connection reset")

    from agent_host_client.hosts import HostConfig, HostRuntime
    from agent_host_client.hosts.policy import Backoff

    runtime = HostRuntime(
        HostConfig(
            flaky_factory,
            label="h",
            reconnect_policy=ReconnectPolicy(
                backoff=Backoff("immediate"), jitter=0.0, max_attempts=3
            ),
        )
    )
    await runtime.start(wait=False)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if runtime.state.status == "failed":
            break
    assert attempts > 1
    await runtime.shutdown()


# ── item 4: TLS configuration ────────────────────────────────────────────────


def test_connect_accepts_an_ssl_context() -> None:
    """An SSLContext is the standard currency for a private CA or a client
    certificate, and it is the whole interface -- a `verify=False` flag would be
    this library taking a position on certificate trust."""
    import inspect

    from agent_host_client.ws.transport import WebSocketClientTransport

    signature = inspect.signature(WebSocketClientTransport.connect)
    assert "ssl" in signature.parameters
    assert signature.parameters["ssl"].annotation in {"SSLContext | None", ssl.SSLContext | None}

    signature = inspect.signature(connect)
    assert "ssl" in signature.parameters
