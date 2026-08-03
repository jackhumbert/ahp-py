"""The broadcast primitive, and the lossless/lossy split ADR 0002 rests on."""

from __future__ import annotations

import asyncio

import pytest

from agent_host_client.client.queue import BroadcastQueue


async def test_readers_are_independent() -> None:
    queue: BroadcastQueue[int] = BroadcastQueue()
    a, b = queue.reader(), queue.reader()
    queue.publish(1)
    queue.publish(2)
    assert [await a.__anext__(), await a.__anext__()] == [1, 2]
    assert [await b.__anext__(), await b.__anext__()] == [1, 2]


async def test_a_late_reader_sees_no_replay() -> None:
    """Attaching is not subscribing to history; a subscription's history is a
    snapshot, and conflating them would let a reader observe state twice."""
    queue: BroadcastQueue[int] = BroadcastQueue()
    queue.publish(1)
    late = queue.reader()
    queue.publish(2)
    assert await late.__anext__() == 2


async def test_unbounded_never_drops() -> None:
    """The default for per-channel subscriptions. A dropped ActionEnvelope
    desyncs the mirror permanently -- there is no re-request for one."""
    queue: BroadcastQueue[int] = BroadcastQueue(0)
    reader = queue.reader()
    for i in range(10_000):
        queue.publish(i)
    received = [await reader.__anext__() for _ in range(10_000)]
    assert received == list(range(10_000))


async def test_bounded_drops_oldest_and_reports_it() -> None:
    """A tap that silently skips is the same failure one layer up."""
    dropped: list[int] = []
    queue: BroadcastQueue[int] = BroadcastQueue(3, on_drop=dropped.append)
    reader = queue.reader()
    for i in range(6):
        queue.publish(i)
    assert sum(dropped) == 3
    assert [await reader.__anext__() for _ in range(3)] == [3, 4, 5]


async def test_a_lagging_reader_is_fast_forwarded_not_left_behind() -> None:
    """Left behind the buffer base a cursor goes negative and yields garbage."""
    queue: BroadcastQueue[int] = BroadcastQueue(2)
    reader = queue.reader()
    for i in range(5):
        queue.publish(i)
    assert await reader.__anext__() == 3


async def test_close_ends_readers_after_they_drain() -> None:
    queue: BroadcastQueue[int] = BroadcastQueue()
    reader = queue.reader()
    queue.publish(1)
    queue.close()
    assert await reader.__anext__() == 1
    with pytest.raises(StopAsyncIteration):
        await reader.__anext__()


async def test_closing_a_reader_is_terminal_even_with_unread_values() -> None:
    queue: BroadcastQueue[int] = BroadcastQueue()
    reader = queue.reader()
    queue.publish(1)
    await reader.aclose()
    with pytest.raises(StopAsyncIteration):
        await reader.__anext__()


async def test_a_parked_reader_wakes_on_publish() -> None:
    queue: BroadcastQueue[int] = BroadcastQueue()
    reader = queue.reader()
    task = asyncio.get_running_loop().create_task(reader.__anext__())
    await asyncio.sleep(0)
    queue.publish(7)
    assert await asyncio.wait_for(task, 1) == 7


async def test_a_parked_reader_wakes_on_close() -> None:
    queue: BroadcastQueue[int] = BroadcastQueue()
    reader = queue.reader()
    task = asyncio.get_running_loop().create_task(reader.__anext__())
    await asyncio.sleep(0)
    queue.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(task, 1)


async def test_the_buffer_is_reclaimed_once_every_cursor_has_passed() -> None:
    """Otherwise an unbounded queue pins every value it ever carried, which is
    the failure mode that would make ADR 0002 untenable."""
    queue: BroadcastQueue[int] = BroadcastQueue()
    reader = queue.reader()
    for i in range(100):
        queue.publish(i)
        await reader.__anext__()
    assert len(queue) == 0


async def test_a_reader_less_bounded_queue_stays_empty_and_reports_no_drops() -> None:
    """Buffering with nobody attached pins memory forever -- a late reader sees
    no replay -- and once a bounded queue filled, every publish fired `on_drop`,
    diagnosing a "reader fell behind" for a reader that never existed."""
    dropped: list[int] = []
    queue: BroadcastQueue[int] = BroadcastQueue(3, on_drop=dropped.append)
    for i in range(10):
        queue.publish(i)
    assert len(queue) == 0
    assert dropped == []
    reader = queue.reader()
    queue.publish(10)
    assert await reader.__anext__() == 10


async def test_detaching_the_last_reader_frees_the_whole_buffer() -> None:
    queue: BroadcastQueue[int] = BroadcastQueue()
    reader = queue.reader()
    for i in range(100):
        queue.publish(i)
    assert len(queue) == 100
    await reader.aclose()
    assert len(queue) == 0
