"""The host-initiated request registry.

Two classes of failure are guarded here, and neither shows up as a wrong answer.

The first is a **parked waiter**: a request whose peer never answers -- because
the connection died, because the client dropped the frame, because nobody is
there -- leaves a provider blocked on a future forever and the registry growing
for the life of the host.

The second is an **escaping exception**. A response is an inbound frame with no
`method`, so there is no reply that could carry an error; anything `resolve`
raises ends `Host.serve`'s read loop and drops a live connection over one
malformed frame from an untrusted peer (invariant 17). Every hostile shape below
must come back as a bool.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from agent_host_protocol.errors import AhpError

from agent_host_server.core.outbound import OutboundRequests

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeConnection:
    """Stands in for `Connection`: identity-keyed, and `send` is a queue append."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[Mapping[str, Any]] = []

    def enqueue(self, message: Mapping[str, Any]) -> None:
        self.sent.append(message)

    @property
    def last_id(self) -> Any:
        return self.sent[-1]["id"]

    def __repr__(self) -> str:
        return f"<FakeConnection {self.name}>"


async def _in_flight(
    outbound: OutboundRequests,
    connection: FakeConnection,
    method: str = "resourceRead",
    params: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> asyncio.Task[Any]:
    """Start a `call` and return once its request has reached the wire."""
    task = asyncio.create_task(
        outbound.call(connection, method, params, send=connection.enqueue, **kwargs)
    )
    while not connection.sent:
        await asyncio.sleep(0)
    return task


# ─── the happy path, and its framing ─────────────────────────────────────


async def test_call_frames_a_jsonrpc_request_and_hands_it_to_send() -> None:
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection, "resourceRead", {"uri": "virtual://c/plugin"})

    assert connection.sent == [
        {
            "jsonrpc": "2.0",
            "id": connection.last_id,
            "method": "resourceRead",
            "params": {"uri": "virtual://c/plugin"},
        }
    ]
    outbound.resolve({"jsonrpc": "2.0", "id": connection.last_id, "result": {"data": "x"}})
    assert await task == {"data": "x"}


async def test_a_call_with_no_params_omits_the_key_entirely() -> None:
    """An explicit `"params": null` is not the same frame as an absent key, and
    a receiver is entitled to reject it."""
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection, "ping")

    assert "params" not in connection.sent[0]
    outbound.resolve({"id": connection.last_id, "result": None})
    assert await task is None


async def test_an_answered_call_leaves_nothing_in_the_registry() -> None:
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection)
    assert len(outbound) == 1

    outbound.resolve({"id": connection.last_id, "result": 1})
    await task
    assert len(outbound) == 0


async def test_a_null_result_is_a_result_not_a_malformed_frame() -> None:
    """`"result": null` is how a method that returns nothing answers. Deciding
    on truthiness -- or on `is None` -- turns every such answer into a fault."""
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection)

    assert outbound.resolve({"id": connection.last_id, "result": None}) is True
    assert await task is None


# ─── routing ─────────────────────────────────────────────────────────────


async def test_a_response_wakes_only_the_request_it_names() -> None:
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    opened = [outbound.open(connection) for _ in range(3)]

    assert outbound.resolve({"id": opened[1][0], "result": "second"}) is True
    assert await opened[1][1] == "second"
    assert not opened[0][1].done(), "a different request was resolved as collateral"
    assert not opened[2][1].done(), "a different request was resolved as collateral"
    assert len(outbound) == 2


async def test_a_response_for_an_unknown_id_is_not_ours_and_does_not_raise() -> None:
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    outbound.open(connection)

    assert outbound.resolve({"id": "ahs-9999", "result": 1}) is False
    assert len(outbound) == 1, "an unknown response disturbed a live request"


async def test_a_frame_whose_id_is_any_other_json_value_is_not_ours() -> None:
    """`{}` and `[]` are legal JSON ids and unhashable in Python: looking one up
    in the registry raises TypeError, in the read loop, over one peer frame."""
    outbound = OutboundRequests()
    outbound.open(FakeConnection("a"))

    for hostile in ({"nope": True}, ["a"], 1, 0, True, None, 1.5):
        assert outbound.resolve({"id": hostile, "result": 1}) is False
    assert outbound.resolve({"result": 1}) is False, "an id-less frame was claimed"
    assert len(outbound) == 1


async def test_resolving_the_same_response_twice_finds_nothing_the_second_time() -> None:
    """A peer can repeat a frame. The second one must find nothing rather than
    raise InvalidStateError on a future that is already done."""
    outbound = OutboundRequests()
    request_id, future = outbound.open(FakeConnection("a"))

    assert outbound.resolve({"id": request_id, "result": 1}) is True
    assert outbound.resolve({"id": request_id, "result": 2}) is False
    assert await future == 1


async def test_a_response_on_a_different_connection_cannot_resolve_the_request() -> None:
    """Ids are sequential, so they are guessable. One peer must not be able to
    answer a request the host addressed to another."""
    outbound = OutboundRequests()
    mine, theirs = FakeConnection("a"), FakeConnection("b")
    task = await _in_flight(outbound, mine)

    assert outbound.resolve({"id": mine.last_id, "result": "forged"}, theirs) is False
    assert len(outbound) == 1
    assert outbound.resolve({"id": mine.last_id, "result": "genuine"}, mine) is True
    assert await task == "genuine"


# ─── errors the peer sends ───────────────────────────────────────────────


async def test_an_error_response_raises_ahp_error_with_the_peers_code() -> None:
    """The code is a decision the caller acts on -- PermissionDenied from a
    reverse resourceRequest is an answer, not a fault -- so it propagates
    verbatim rather than being flattened or returned."""
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection)

    outbound.resolve(
        {
            "id": connection.last_id,
            "error": {"code": -32009, "message": "Denied by the user", "data": {"read": True}},
        }
    )
    with pytest.raises(AhpError) as caught:
        await task
    assert caught.value.code == -32009
    assert caught.value.message == "Denied by the user"
    assert caught.value.data == {"read": True}
    assert len(outbound) == 0


async def test_a_boolean_error_code_does_not_become_the_code() -> None:
    """`isinstance(True, int)` is true in Python, so `"code": true` would be
    carried through and re-serialised as JSON `true` -- not a code at all."""
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection)

    outbound.resolve({"id": connection.last_id, "error": {"code": True, "message": "?"}})
    with pytest.raises(AhpError) as caught:
        await task
    assert caught.value.code == -32603


async def test_a_malformed_error_object_still_wakes_the_caller() -> None:
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection)

    assert outbound.resolve({"id": connection.last_id, "error": "nope"}) is True
    with pytest.raises(AhpError) as caught:
        await task
    assert caught.value.code == -32603
    assert len(outbound) == 0


@pytest.mark.parametrize(
    ("frame", "why"),
    [
        ({"result": 1, "error": {"code": -1, "message": "x"}}, "both result and error"),
        ({}, "neither result nor error"),
    ],
)
async def test_a_malformed_response_fails_the_caller_not_the_read_loop(
    frame: Mapping[str, Any], why: str
) -> None:
    """It is still an answer to a request we can name, so the waiter is woken
    and the frame counts as consumed -- `resolve` returning False here would
    make the read loop log it as a stray and leave the caller parked forever."""
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection)

    assert outbound.resolve({"id": connection.last_id, **frame}) is True, why
    with pytest.raises(AhpError) as caught:
        await task
    assert caught.value.code == -32603
    assert len(outbound) == 0


# ─── lifetimes ───────────────────────────────────────────────────────────


async def test_a_dropped_connection_wakes_every_waiter_on_it() -> None:
    outbound = OutboundRequests()
    doomed, survivor = FakeConnection("a"), FakeConnection("b")
    first = await _in_flight(outbound, doomed)
    doomed.sent.clear()
    second = await _in_flight(outbound, doomed)
    third = await _in_flight(outbound, survivor)

    assert outbound.fail_connection(doomed, "transport closed") == 2
    for task in (first, second):
        with pytest.raises(AhpError) as caught:
            await task
        assert caught.value.code == -32603
        assert "transport closed" in caught.value.message

    assert not third.done(), "another connection's request was collateral"
    assert len(outbound) == 1
    outbound.resolve({"id": survivor.last_id, "result": "fine"})
    assert await third == "fine"


async def test_failing_a_connection_is_idempotent() -> None:
    """Teardown paths overlap -- the read loop's finally, an explicit close, a
    transport error -- and the second one must not raise on a done future."""
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection)

    assert outbound.fail_connection(connection, "closed") == 1
    assert outbound.fail_connection(connection, "closed again") == 0
    assert outbound.fail_connection(FakeConnection("never used"), "closed") == 0
    with pytest.raises(AhpError):
        await task
    assert len(outbound) == 0


async def test_a_timeout_wakes_the_caller_and_leaves_nothing_behind() -> None:
    """A machine is on the other end, unlike ADR 0005's human-facing requests,
    so silence is a failure rather than someone thinking."""
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection, timeout=0.01)

    with pytest.raises(AhpError) as caught:
        await task
    assert caught.value.code == -32603
    assert "within" in caught.value.message
    assert len(outbound) == 0


async def test_a_timeout_of_none_waits() -> None:
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection, timeout=None)

    await asyncio.sleep(0.02)
    assert not task.done(), "an overridable timeout was not overridden"
    outbound.resolve({"id": connection.last_id, "result": "late"})
    assert await task == "late"


async def test_a_cancelled_caller_leaves_nothing_behind() -> None:
    """The turn was cancelled while the client was still thinking. The registry
    entry has to go with it, or the next response for that id resolves a future
    nobody owns."""
    outbound = OutboundRequests()
    connection = FakeConnection("a")
    task = await _in_flight(outbound, connection)
    assert len(outbound) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(outbound) == 0
    assert outbound.resolve({"id": connection.last_id, "result": 1}) is False


async def test_a_send_that_fails_leaves_nothing_behind() -> None:
    """The frame never reached the socket, so nothing will ever answer it."""
    outbound = OutboundRequests()

    def explode(message: Mapping[str, Any]) -> None:
        raise RuntimeError("transport gone")

    with pytest.raises(RuntimeError):
        await outbound.call(FakeConnection("a"), "resourceRead", send=explode)
    assert len(outbound) == 0


# ─── ids ─────────────────────────────────────────────────────────────────


async def test_ids_are_strings_so_they_cannot_collide_with_a_clients() -> None:
    """The published client mints its request ids with `this.nextRequestId++`
    (client/client.ts) -- always a JSON number, on the same socket. A JSON
    string is never a JSON number, in either language's equality."""
    outbound = OutboundRequests()
    minted = [outbound.next_id() for _ in range(50)]

    assert all(isinstance(request_id, str) for request_id in minted)
    assert len(set(minted)) == 50, "an id was reused"


async def test_a_numeric_id_is_never_ours_even_at_the_same_ordinal() -> None:
    """The client's request #1 and our request #1 share a socket. If the client
    answers its own id-1 request, or asks about it, that frame must not touch
    ours."""
    outbound = OutboundRequests()
    request_id, _ = outbound.open(FakeConnection("a"))

    assert request_id == "ahs-1"
    assert outbound.resolve({"id": 1, "result": "the client's own"}) is False
    assert len(outbound) == 1


async def test_ids_do_not_repeat_across_connections() -> None:
    """One counter per registry, not per connection: it is what makes the
    per-connection index exact and lets a response name exactly one request."""
    outbound = OutboundRequests()
    first, _ = outbound.open(FakeConnection("a"))
    second, _ = outbound.open(FakeConnection("b"))

    assert first != second
