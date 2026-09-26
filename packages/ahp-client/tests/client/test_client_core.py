"""The single-shot client's invariants.

Each of these is a behaviour a host was written against, or a distinction a
caller keys a retry decision on. They are grouped by the thing that breaks when
one is wrong, not by method name.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_client.client import (
    ActionEvent,
    AhpClient,
    ClientClosed,
    ClientConfig,
    ProtocolVersionError,
    RequestTimeout,
    RpcError,
    SessionAdded,
    TransportError,
)
from agent_host_client.client.errors import (
    MethodNotFound,
    NotFound,
    UnsupportedProtocolVersion,
    is_session_gone,
)
from agent_host_client.client.events import (
    MalformedFrame,
    OtlpEvent,
    ProgressEvent,
    UnknownResponse,
)
from agent_host_client.testing import FakeRpcError, echo_host


class _EagerTransport:
    """A transport that parses eagerly, the way a third-party one may.

    Neither shipped transport used to raise ``json.JSONDecodeError`` from
    ``receive()``, which is exactly why the client's handling of one was never
    exercised -- and was wrong.
    """

    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, Any] | Exception | None] = asyncio.Queue()
        self.closed = False

    async def send(self, message: Mapping[str, Any]) -> None:
        return None

    async def receive(self) -> dict[str, Any] | None:
        item = await self.frames.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True
        self.frames.put_nowait(None)


async def _connected(**config: Any) -> tuple[AhpClient, Any]:
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport(), ClientConfig(**config))
    await client.connect()
    return client, host


# ── handshake ────────────────────────────────────────────────────────────────


async def test_initialize_sends_client_info_and_capabilities() -> None:
    """The reference helper cannot send either, despite the types supporting them."""
    client, host = await _connected(
        client_info={"name": "agent-host-client-py", "version": "0"},
        capabilities={},
    )
    await client.initialize(client_id="c1", initial_subscriptions=[ROOT_URI])
    sent = next(m for m in host.received if m.get("method") == "initialize")
    assert sent["params"]["clientInfo"] == {"name": "agent-host-client-py", "version": "0"}
    assert sent["params"]["capabilities"] == {}
    await client.shutdown()
    await host.stop()


async def test_absent_optionals_are_omitted_not_null() -> None:
    """`json.dumps` writes null where `JSON.stringify` drops the key.

    A host distinguishing "absent" from "explicitly null" -- and the reducers do
    -- sees a different message than the reference client would have sent.
    """
    client, host = await _connected()
    await client.initialize(client_id="c1")
    params = next(m for m in host.received if m.get("method") == "initialize")["params"]
    for key in ("clientInfo", "capabilities", "locale", "initialSubscriptions"):
        assert key not in params, f"{key} should be absent, not null"
    await client.shutdown()
    await host.stop()


async def test_negotiated_version_is_verified() -> None:
    """The reference client accepts a version it never offered. Measured, not assumed."""
    client, host = await _connected(protocol_versions=("0.7.0",))
    host.protocol_version = "0.3.0"
    with pytest.raises(ProtocolVersionError) as caught:
        await client.initialize(client_id="c1")
    assert caught.value.negotiated == "0.3.0"
    assert caught.value.offered == ("0.7.0",)
    await client.shutdown()
    await host.stop()


async def test_version_verification_can_be_switched_off_for_a_loose_host() -> None:
    client, host = await _connected(protocol_versions=("0.7.0",), verify_negotiated_version=False)
    host.protocol_version = "0.3.0"
    result = await client.initialize(client_id="c1")
    assert result["protocolVersion"] == "0.3.0"
    await client.shutdown()
    await host.stop()


async def test_offered_versions_default_to_what_the_pin_covers() -> None:
    """Not upstream's constant, which advertises versions we have no tables for."""
    from agent_host_protocol.types import UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS

    client, host = await _connected()
    await client.initialize(client_id="c1")
    offered = next(m for m in host.received if m.get("method") == "initialize")["params"][
        "protocolVersions"
    ]
    assert offered == ["0.9.0", "0.8.0", "0.7.0", "0.6.0"]
    assert set(offered) < set(UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS)
    await client.shutdown()
    await host.stop()


# ── request correlation ──────────────────────────────────────────────────────


async def test_request_ids_do_not_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """VS Code's first frame on a fresh socket carried id 66.

    A peer must not assume per-connection numbering, and neither do we.
    """
    client, host = await _connected()
    await client.ping()
    await client.ping()
    ids = [m["id"] for m in host.received if m.get("method") == "ping"]
    assert ids == [1, 2]
    await client.shutdown()
    await host.stop()


async def test_error_responses_become_typed_exceptions() -> None:
    client, host = await _connected()
    host.on(
        "createSession",
        lambda _p: (_ for _ in ()).throw(
            FakeRpcError({"code": -32002, "message": "no such provider"})
        ),
    )
    with pytest.raises(RpcError) as caught:
        await client.request("createSession", {"channel": "x", "provider": "nope"})
    assert caught.value.code == -32002
    await client.shutdown()
    await host.stop()


async def test_unimplemented_method_is_an_answer_not_a_fault() -> None:
    """AHP has no capability object, so -32601 is how a host declines."""
    client, host = await _connected()
    with pytest.raises(MethodNotFound):
        await client.request("invokeChangesetOperation", {"channel": ROOT_URI})
    await client.shutdown()
    await host.stop()


async def test_timeout_is_not_an_rpc_error() -> None:
    """No peer error occurred; the wait elapsed. Callers retry one and not the other."""
    client, host = await _connected(request_timeout=0.05)
    host.on("listSessions", lambda _p: asyncio.sleep(5))
    with pytest.raises(RequestTimeout) as caught:
        await client.request("listSessions", {"channel": ROOT_URI})
    assert not isinstance(caught.value, RpcError)
    assert caught.value.method == "listSessions"
    await client.shutdown()
    await host.stop()


async def test_cancelling_a_request_does_not_leave_a_resolvable_entry() -> None:
    """A late response for an abandoned id must not resolve a future nobody reads.

    asyncio reports that as an unretrieved exception, at an unrelated moment,
    with no stack that points here.
    """
    client, host = await _connected(request_timeout=None)
    host.on("listSessions", lambda _p: asyncio.sleep(0.2))
    task = asyncio.get_running_loop().create_task(
        client.request("listSessions", {"channel": ROOT_URI})
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client._pending == {}
    await client.shutdown()
    await host.stop()


async def test_cancelling_a_timed_request_pops_the_entry_and_drops_the_late_response() -> None:
    """The default path runs under `asyncio.shield`, and a caller's cancellation
    stops there: the inner future stayed pending, the finally's guard saw
    nothing, and the entry leaked until tear-down -- where a late response
    settled a future nobody retrieves instead of firing `UnknownResponse`."""
    client, host = await _connected(request_timeout=30.0)
    release = asyncio.Event()

    async def slow(_p: dict[str, Any]) -> dict[str, Any]:
        await release.wait()
        return {}

    host.on("listSessions", slow)
    diagnostics = client.diagnostics()
    task = asyncio.get_running_loop().create_task(
        client.request("listSessions", {"channel": ROOT_URI})
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client._pending == {}
    release.set()  # the id is burned; the late response is dropped, with a diagnostic
    diagnostic = await asyncio.wait_for(diagnostics.__anext__(), 1)
    assert isinstance(diagnostic, UnknownResponse)
    assert client.connection_state.status == "connected"
    await client.shutdown()
    await host.stop()


async def test_counter_seeds_let_a_successor_continue_the_predecessors_numbering() -> None:
    """One client is one transport, so "ids never reset across transport swaps"
    is the supervisor's promise to keep -- these seeds are how it can."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport(), first_request_id=66, first_client_seq=42)
    await client.connect()
    await client.ping()
    handle = client.dispatch("ahp-chat:/c", {"type": "chat/draftChanged", "draft": "x"})
    assert next(m for m in host.received if m.get("method") == "ping")["id"] == 66
    assert handle.client_seq == 42
    assert client.next_request_id == 67
    assert client.next_client_seq == 43
    await client.shutdown()
    await host.stop()


# ── lifecycle ────────────────────────────────────────────────────────────────


async def test_shutdown_fails_pending_with_client_closed_not_transport_error() -> None:
    """Tear-down happens before the transport closes, so the cause is honest."""
    client, host = await _connected(request_timeout=None)
    host.on("listSessions", lambda _p: asyncio.sleep(5))
    task = asyncio.get_running_loop().create_task(
        client.request("listSessions", {"channel": ROOT_URI})
    )
    await asyncio.sleep(0.01)
    await client.shutdown()
    with pytest.raises(ClientClosed):
        await task
    await host.stop()


async def test_state_sequence_is_connected_closing_closed() -> None:
    client, host = await _connected()
    seen: list[str] = []
    reader = client.state_changes()

    async def collect() -> None:
        async for state in reader:
            seen.append(state.status)

    task = asyncio.get_running_loop().create_task(collect())
    await asyncio.sleep(0)
    await client.shutdown()
    await asyncio.sleep(0.01)
    task.cancel()
    assert seen == ["closing", "closed"]
    await host.stop()


async def test_unsubscribe_after_shutdown_is_a_no_op_while_everything_else_raises() -> None:
    """A caller tidying up is not making a mistake; a caller still dispatching is."""
    client, host = await _connected()
    await client.shutdown()
    await client.unsubscribe(ROOT_URI)  # must not raise
    with pytest.raises(ClientClosed):
        client.dispatch(ROOT_URI, {"type": "root/agentsChanged", "agents": []})
    with pytest.raises(ClientClosed):
        client.notify("unsubscribe", {"channel": ROOT_URI})
    with pytest.raises(ClientClosed):
        await client.request("ping", {"channel": ROOT_URI})
    await host.stop()


async def test_transport_close_tears_down_with_a_transport_error() -> None:
    client, host = await _connected()
    await host.stop()
    await asyncio.sleep(0.02)
    assert client.connection_state.status == "closed"
    assert isinstance(client.connection_state.error, TransportError)


async def test_shutdown_reaps_the_writer_after_the_read_loop_tore_down() -> None:
    """`shutdown()` is the only tear-down the supervisor calls, and it must
    reap the tasks however the connection ended. Early-returning on the state
    the read loop already set left `_write_loop` parked on the outbox forever,
    holding this client and its transport -- once per reconnect."""

    def running() -> list[str]:
        return sorted(t.get_name() for t in asyncio.all_tasks() if "ahp-client" in t.get_name())

    client, host = await _connected()
    assert running() == ["ahp-client-read", "ahp-client-write"]
    await host.stop()
    await asyncio.sleep(0.02)
    assert client.connection_state.status == "closed"
    await client.shutdown()
    await asyncio.sleep(0.02)
    assert running() == []
    await client.shutdown()  # still idempotent


# ── subscriptions ────────────────────────────────────────────────────────────


async def test_subscribe_attaches_before_sending() -> None:
    """Nothing delivered during the round trip may be lost."""
    client, host = await _connected()
    chat = "ahp-chat:/c"

    async def slow_subscribe(params: dict[str, Any]) -> dict[str, Any]:
        # Push an action *while the subscribe is still in flight*. A client that
        # attaches after the response would drop it.
        await host.push(chat, {"type": "chat/turnStarted", "turnId": "t1"})
        return {"snapshot": {"resource": chat, "state": {"turns": []}, "fromSeq": 1}}

    host.on("subscribe", slow_subscribe)
    _result, subscription = await client.subscribe(chat)
    event = await asyncio.wait_for(subscription.__anext__(), 1)
    assert isinstance(event, ActionEvent)
    assert event.action["turnId"] == "t1"
    await client.shutdown()
    await host.stop()


async def test_failed_subscribe_closes_the_local_queue() -> None:
    """Rust leaks it here; Go and Swift roll back, and so do we."""
    client, host = await _connected()
    host.on(
        "subscribe",
        lambda _p: (_ for _ in ()).throw(FakeRpcError({"code": -32009, "message": "denied"})),
    )
    with pytest.raises(RpcError):
        await client.subscribe("ahp-chat:/nope")
    assert "ahp-chat:/nope" not in client._subscriptions
    await client.shutdown()
    await host.stop()


async def test_two_subscriptions_on_one_uri_both_see_every_event() -> None:
    client, host = await _connected()
    first = client.attach_subscription(ROOT_URI)
    second = client.attach_subscription(ROOT_URI)
    await host.notify("root/sessionAdded", {"channel": ROOT_URI, "summary": {"resource": "s:/1"}})
    a = await asyncio.wait_for(first.__anext__(), 1)
    b = await asyncio.wait_for(second.__anext__(), 1)
    assert isinstance(a, SessionAdded)
    assert isinstance(b, SessionAdded)
    await client.shutdown()
    await host.stop()


# ── dispatch ─────────────────────────────────────────────────────────────────


async def test_dispatch_is_synchronous_so_client_seq_cannot_reorder() -> None:
    """If `dispatch` awaited, two coroutines could interleave between allocating
    the sequence and enqueueing, putting 5 on the wire before 4."""
    client, host = await _connected()

    async def spam(start: int) -> None:
        for _ in range(20):
            client.dispatch("ahp-chat:/c", {"type": "chat/draftChanged", "draft": str(start)})
            await asyncio.sleep(0)

    await asyncio.gather(spam(0), spam(1), spam(2))
    await asyncio.sleep(0.05)
    seqs = [m["params"]["clientSeq"] for m in host.received if m.get("method") == "dispatchAction"]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs)) == 60
    await client.shutdown()
    await host.stop()


async def test_explicit_client_seq_advances_the_counter() -> None:
    client, host = await _connected()
    client.dispatch("c", {"type": "x"}, 41)
    handle = client.dispatch("c", {"type": "x"})
    assert handle.client_seq == 42
    await client.shutdown()
    await host.stop()


# ── inbound requests ─────────────────────────────────────────────────────────


async def test_no_handler_answers_method_not_found_so_the_host_does_not_leak() -> None:
    client, host = await _connected()
    with pytest.raises(FakeRpcError) as caught:
        await asyncio.wait_for(host.call("resourceRead", {"uri": "file:///x"}), 1)
    assert caught.value.error["code"] == -32601
    await client.shutdown()
    await host.stop()


async def test_handler_result_is_returned_to_the_host() -> None:
    client, host = await _connected()

    async def handler(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        assert method == "resourceRead"
        return {"content": "hello", "uri": params["uri"]}

    client.set_server_request_handler(handler)
    result = await asyncio.wait_for(host.call("resourceRead", {"uri": "file:///x"}), 1)
    assert result == {"content": "hello", "uri": "file:///x"}
    await client.shutdown()
    await host.stop()


async def test_handler_may_reenter_the_client_without_deadlocking() -> None:
    """Inbound requests run on their own task; answering inline would deadlock.

    A resource server resolving a `ContentRef` really does call back out.
    """
    client, host = await _connected()

    async def handler(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        await client.ping()
        return {"ok": True}

    client.set_server_request_handler(handler)
    assert await asyncio.wait_for(host.call("resourceResolve", {"uri": "file:///x"}), 1) == {
        "ok": True
    }
    await client.shutdown()
    await host.stop()


async def test_handler_exception_becomes_internal_error_not_a_dropped_request() -> None:
    client, host = await _connected()

    async def handler(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        raise ValueError("boom")

    client.set_server_request_handler(handler)
    with pytest.raises(FakeRpcError) as caught:
        await asyncio.wait_for(host.call("resourceRead", {"uri": "file:///x"}), 1)
    assert caught.value.error["code"] == -32603
    assert "boom" in caught.value.error["message"]
    await client.shutdown()
    await host.stop()


# ── notifications ────────────────────────────────────────────────────────────


async def test_all_nine_server_notifications_reach_events() -> None:
    """The reference client handles five and drops the rest at a `default:`
    branch that reaches neither subscriptions nor `events()`."""
    client, host = await _connected()
    tap = client.events()
    channel = "ahp-chat:/c"

    await host.push(channel, {"type": "chat/turnStarted", "turnId": "t"})
    for method in (
        "root/sessionAdded",
        "root/sessionRemoved",
        "root/sessionSummaryChanged",
        "auth/required",
        "root/progress",
        "otlp/exportLogs",
        "otlp/exportTraces",
        "otlp/exportMetrics",
    ):
        await host.notify(method, {"channel": channel})

    seen = [await asyncio.wait_for(tap.__anext__(), 1) for _ in range(9)]
    kinds = [type(e.event).__name__ for e in seen]
    assert kinds.count("OtlpEvent") == 3
    assert "ProgressEvent" in kinds
    assert isinstance(seen[0].event, ActionEvent)
    assert isinstance(seen[5].event, ProgressEvent)
    assert isinstance(seen[6].event, OtlpEvent)
    assert seen[6].event.signal == "logs"
    await client.shutdown()
    await host.stop()


async def test_server_seq_tracks_the_maximum_seen() -> None:
    client, host = await _connected()
    client.attach_subscription("c")
    await host.push("c", {"type": "chat/turnStarted", "turnId": "t"}, server_seq=40)
    await host.push("c", {"type": "chat/turnComplete", "turnId": "t"}, server_seq=12)
    await asyncio.sleep(0.02)
    assert client.last_seen_server_seq == 40
    await client.shutdown()
    await host.stop()


async def test_unknown_notification_is_ignored_without_killing_the_read_loop() -> None:
    client, host = await _connected()
    await host.notify("some/futureNotification", {"channel": "c"})
    await asyncio.sleep(0.01)
    assert client.connection_state.status == "connected"
    await client.ping()
    await client.shutdown()
    await host.stop()


# ── malformed input ──────────────────────────────────────────────────────────


async def test_one_malformed_frame_does_not_kill_in_flight_requests() -> None:
    client_side, host_side = memory_pair()
    client = AhpClient(client_side, ClientConfig(request_timeout=1.0))
    await client.connect()
    diagnostics = client.diagnostics()

    await host_side.send({"nonsense": True})
    diagnostic = await asyncio.wait_for(diagnostics.__anext__(), 1)
    assert isinstance(diagnostic, MalformedFrame)
    assert client.connection_state.status == "connected"
    await client.shutdown()


async def test_a_flood_of_malformed_frames_gives_up() -> None:
    """A peer streaming garbage is not recovering, and holding the socket open
    only delays every caller's timeout."""
    client_side, host_side = memory_pair()
    client = AhpClient(client_side)
    await client.connect()
    for _ in range(8):
        await host_side.send({"nonsense": True})
    await asyncio.sleep(0.02)
    assert client.connection_state.status == "closed"
    assert isinstance(client.connection_state.error, TransportError)
    assert client.connection_state.error.kind == "protocol"
    # Tear-down alone settles futures but held the socket open; a bare client
    # has no supervisor to close it, so giving up must close it too.
    assert await asyncio.wait_for(host_side.receive(), 1) is None


async def test_a_decode_error_from_an_eager_transport_does_not_end_the_read_loop() -> None:
    """The `JSONDecodeError` handler used to sit outside the while loop: one bad
    frame returned from the coroutine, leaving the state "connected" with
    nobody reading -- every later request waited out its full timeout."""
    transport = _EagerTransport()
    client = AhpClient(transport, ClientConfig(request_timeout=1.0))
    await client.connect()
    diagnostics = client.diagnostics()
    tap = client.events()

    transport.frames.put_nowait(json.JSONDecodeError("bad frame", "{", 0))
    transport.frames.put_nowait(
        {
            "jsonrpc": "2.0",
            "method": "root/sessionAdded",
            "params": {"channel": ROOT_URI, "summary": {"resource": "s:/1"}},
        }
    )
    diagnostic = await asyncio.wait_for(diagnostics.__anext__(), 1)
    assert isinstance(diagnostic, MalformedFrame)
    event = await asyncio.wait_for(tap.__anext__(), 1)
    assert isinstance(event.event, SessionAdded)
    assert client.connection_state.status == "connected"
    await client.shutdown()


async def test_a_flood_of_decode_errors_gives_up_and_closes_the_transport() -> None:
    transport = _EagerTransport()
    client = AhpClient(transport)
    await client.connect()
    for _ in range(8):
        transport.frames.put_nowait(json.JSONDecodeError("bad frame", "{", 0))
    await asyncio.sleep(0.02)
    assert client.connection_state.status == "closed"
    assert isinstance(client.connection_state.error, TransportError)
    assert client.connection_state.error.kind == "protocol"
    assert transport.closed


async def test_an_unexpected_reader_failure_surfaces_on_the_connection_state() -> None:
    """The class docstring promises done-callbacks that surface failures. A
    discard-only callback swallowed the exception, and the reader died with the
    state stuck "connected" -- the silent-hang class, not a tear-down."""
    transport = _EagerTransport()
    client = AhpClient(transport)
    await client.connect()
    transport.frames.put_nowait(RuntimeError("not in the read loop's caught set"))
    await asyncio.sleep(0.02)
    assert client.connection_state.status == "closed"
    assert isinstance(client.connection_state.error, TransportError)
    assert client.connection_state.error.kind == "protocol"
    await client.shutdown()


async def test_a_raising_mirror_costs_one_frame_not_the_connection() -> None:
    """`_on_notification` documents that an exception must never escape; the
    per-frame guard is what enforces it. One bad envelope becomes a counted
    diagnostic under the malformed-frame policy, not the end of the reader."""
    from agent_host_client.client.mirror import ApplyOutcome, StateMirror

    class _Faulty(StateMirror):
        def apply(self, envelope: Mapping[str, Any]) -> ApplyOutcome:
            raise RuntimeError("reducer went wrong")

    client, host = await _connected()
    client.set_state_mirror(_Faulty(client_id="c1"))
    diagnostics = client.diagnostics()
    await host.push("ahp-chat:/c", {"type": "chat/turnStarted", "turnId": "t"})
    diagnostic = await asyncio.wait_for(diagnostics.__anext__(), 1)
    assert isinstance(diagnostic, MalformedFrame)
    assert client.connection_state.status == "connected"
    await client.ping()  # the connection is still fully usable
    await client.shutdown()
    await host.stop()


async def test_a_boolean_response_id_does_not_settle_request_one() -> None:
    """`isinstance(True, int)` holds and `True == 1` as a dict key, so a frame
    with `"id": true` settled request 1 with the wrong payload before the guard
    excluded bool -- the same discipline as `_absorb_server_seq`."""
    client_side, host_side = memory_pair()
    client = AhpClient(client_side, ClientConfig(request_timeout=1.0))
    await client.connect()
    diagnostics = client.diagnostics()
    task = asyncio.get_running_loop().create_task(client.request("ping", {"channel": ROOT_URI}))
    await asyncio.sleep(0.01)
    await host_side.send({"jsonrpc": "2.0", "id": True, "result": {"wrong": True}})
    diagnostic = await asyncio.wait_for(diagnostics.__anext__(), 1)
    assert isinstance(diagnostic, UnknownResponse)
    assert not task.done()  # request 1 is still waiting for its real answer
    await host_side.send({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert await asyncio.wait_for(task, 1) == {}
    await client.shutdown()


# ── error taxonomy ───────────────────────────────────────────────────────────


def test_supported_versions_reads_the_field_the_schema_names() -> None:
    """`errors.schema.json` and `errors.ts:157` name it `supportedVersions`;
    reading only the longer spelling parsed the one frame that explains a
    handshake failure to an empty tuple against every conformant host."""
    conformant = UnsupportedProtocolVersion(
        -32005, "no", {"supportedVersions": ["0.7.0", ">=0.1.0 <0.3.0"]}
    )
    assert conformant.supported_versions == ("0.7.0", ">=0.1.0 <0.3.0")
    # The shared package's own helper emitted the legacy spelling; tolerated.
    legacy = UnsupportedProtocolVersion(-32005, "no", {"supportedProtocolVersions": ["0.6.0"]})
    assert legacy.supported_versions == ("0.6.0",)
    assert UnsupportedProtocolVersion(-32005, "no", None).supported_versions == ()


def test_is_session_gone_unifies_the_two_codes_hosts_actually_use() -> None:
    """`@wyrd-company/ahp-server` defines NotFound=-32008 and no -32001 at all."""
    assert is_session_gone(RpcError(-32001, "gone"))
    assert is_session_gone(NotFound(-32008, "gone"))
    assert not is_session_gone(RpcError(-32009, "denied"))
    assert not is_session_gone(ValueError("unrelated"))
