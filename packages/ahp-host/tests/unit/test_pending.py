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

    assert {r.id for r in pending.cancel_scope("turn-1", "turn ended")} == {first.id, second.id}
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
    assert PendingRequests().cancel_scope("never-existed", "x") == []


async def test_a_scope_reports_requests_whose_future_was_already_cancelled() -> None:
    """When the TASK is cancelled, the future it was awaiting is cancelled
    before the scope teardown runs. Those requests still have a published
    `session/inputNeeded` entry to retract -- reporting only the ones cancelled
    here left every cancelled turn's session pinned in InputNeeded.
    """
    pending = PendingRequests()
    request = pending.open("turn-1", "clienttool", key="call-1")
    request.future.cancel()  # what task cancellation does to the awaited future

    reported = pending.cancel_scope("turn-1", "turn ended")
    assert [r.id for r in reported] == [request.id]
    assert len(pending) == 0
    assert pending.id_for_key("call-1") is None, "the key index leaked"


class TestKeysAreScopedToTheirChannel:
    """A `toolCallId` is chosen by the provider and unique only within its own
    chat. `EchoProvider` hardcodes `"echo-tool-1"`; any adapter numbering calls
    per turn does the same.

    The registry keyed on the bare id, so two ORDINARY sessions -- no
    capabilities, nothing exotic -- collided: the second park overwrote the
    first's entry, the first session's approval was then refused forever with
    "no tool call awaiting that id", and its chat stayed pinned at InputNeeded
    with a live `session/inputNeeded` until the session was disposed.

    The channel scoping existed, but only on the LOOKUP -- so it filtered a
    collision that had already happened in the store.
    """

    async def test_two_channels_may_use_the_same_tool_call_id(self) -> None:
        pending = PendingRequests()
        first = pending.open("s#t", "confirm", key="echo-tool-1", channel="ahp-chat:/a")
        second = pending.open("s#t", "confirm", key="echo-tool-1", channel="ahp-chat:/b")

        assert first.id != second.id
        assert pending.id_for_key("echo-tool-1", channel="ahp-chat:/a") == first.id
        assert pending.id_for_key("echo-tool-1", channel="ahp-chat:/b") == second.id

    async def test_resolving_one_leaves_the_other(self) -> None:
        pending = PendingRequests()
        first = pending.open("s#t", "confirm", key="dup", channel="ahp-chat:/a")
        pending.open("s#t", "confirm", key="dup", channel="ahp-chat:/b")

        assert pending.resolve(first.id, RequestOutcome(response="accept"))
        assert pending.id_for_key("dup", channel="ahp-chat:/a") is None
        assert pending.id_for_key("dup", channel="ahp-chat:/b") is not None
        assert len(pending) == 1

    async def test_a_channelless_park_is_still_answerable_from_anywhere(self) -> None:
        """A bare sink in a test opens without a channel, and that has to keep
        working -- the scoping is a fix for collisions, not a new requirement."""
        pending = PendingRequests()
        parked = pending.open("s#t", "confirm", key="loose")

        assert pending.id_for_key("loose", channel="ahp-chat:/anything") == parked.id
        assert pending.id_for_key("loose") == parked.id
