"""The suspended-request registry (ADR 0005).

The failure mode this guards is a leak, not a wrong answer: a suspended provider
holds its turn open, so a request that outlives its turn blocks the provider on a
future nobody will ever resolve and leaves the chat in `InputNeeded` until the
session is disposed.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_host_server.core.pending import PendingRequests, RequestOutcome

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_resolving_hands_the_outcome_to_the_waiter() -> None:
    pending = PendingRequests()
    request = pending.open("turn-1", "input")

    async def answer() -> None:
        pending.resolve(request.id, RequestOutcome("accept", {"q": 1}))

    asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(answer()))
    outcome = await request.future
    assert outcome.response == "accept"
    assert outcome.payload == {"q": 1}


async def test_ids_are_minted_here_and_are_unique() -> None:
    """A provider-chosen id lets two providers collide; a client-chosen one lets
    a peer resolve a request it was never offered."""
    pending = PendingRequests()
    ids = {pending.open("turn-1", "input").id for _ in range(50)}
    assert len(ids) == 50


async def test_cancelling_a_scope_frees_every_request_in_it() -> None:
    pending = PendingRequests()
    first = pending.open("turn-1", "input")
    second = pending.open("turn-1", "input")
    other = pending.open("turn-2", "input")

    assert pending.cancel_scope("turn-1", "turn ended") == 2
    assert first.future.cancelled()
    assert second.future.cancelled()
    assert not other.future.done(), "a different turn's request was collateral"


async def test_a_cancelled_scope_leaves_nothing_behind() -> None:
    """The leak is the whole point: a registry that keeps entries after their
    scope dies grows for the life of the host."""
    pending = PendingRequests()
    for _ in range(5):
        pending.open("turn-1", "input")
    pending.cancel_scope("turn-1", "turn ended")
    assert len(pending) == 0


async def test_resolving_twice_is_not_an_error_and_does_not_resolve_twice() -> None:
    """`chat/inputCompleted` is client-dispatchable, so a peer can send it
    again. The second one must find nothing rather than raise."""
    pending = PendingRequests()
    request = pending.open("turn-1", "input")
    assert pending.resolve(request.id, RequestOutcome("accept")) is True
    assert pending.resolve(request.id, RequestOutcome("decline")) is False
    assert (await request.future).response == "accept"


async def test_resolving_an_unknown_id_is_false_not_an_exception() -> None:
    pending = PendingRequests()
    assert pending.resolve("input-999", RequestOutcome("accept")) is False


async def test_is_open_tolerates_any_json_value() -> None:
    """It validates a client-supplied `requestId`, which may be anything --
    including an unhashable dict, which is how a reducer got faulted before."""
    pending = PendingRequests()
    request = pending.open("turn-1", "input")
    assert pending.is_open(request.id)
    for hostile in ({"nope": True}, ["a"], 7, None, "input-does-not-exist"):
        assert not pending.is_open(hostile)


async def test_cancelling_an_unknown_scope_is_a_no_op() -> None:
    assert PendingRequests().cancel_scope("never-existed", "x") == 0
