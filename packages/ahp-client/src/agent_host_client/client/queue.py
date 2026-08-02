"""One publisher, many independent readers.

A port of the reference `AsyncBroadcastQueue` with one deliberate difference:
**a queue may be unbounded, and per-channel subscriptions are** (ADR 0002).
Bounded queues drop the oldest entry and fast-forward the lagging reader past
the gap, which is right for a tap and wrong for a mutation stream.

Readers hold absolute cursors into a shared buffer, so a late reader sees only
what arrives after it attaches -- there is no replay. Entries every cursor has
passed are trimmed, so a queue nobody is behind on does not grow.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Generic, TypeVar

T = TypeVar("T")

__all__ = ["BroadcastQueue", "BroadcastReader"]


class BroadcastQueue(Generic[T]):
    """Fan one stream of values out to any number of readers.

    :param maxsize: ``0`` means unbounded. A positive value evicts the oldest
        entry when full and reports how many were lost through *on_drop*.
    :param on_drop: called with the number of evicted entries. This is how a
        drop becomes a :class:`~agent_host_client.client.events.DroppedEvents`
        diagnostic instead of silence.
    """

    __slots__ = ("_base", "_buffer", "_closed", "_cursors", "_maxsize", "_on_drop", "_waiters")

    def __init__(self, maxsize: int = 0, *, on_drop: Callable[[int], None] | None = None) -> None:
        self._buffer: deque[T] = deque()
        #: Absolute index of ``_buffer[0]``. Cursors are absolute so trimming
        #: the front does not have to touch every reader.
        self._base = 0
        self._cursors: dict[int, int] = {}
        self._waiters: dict[int, asyncio.Future[None]] = {}
        self._maxsize = max(0, maxsize)
        self._on_drop = on_drop
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        """Buffered entries. Depth is the signal that a reader has stalled."""
        return len(self._buffer)

    def publish(self, value: T) -> None:
        """Deliver *value* to every attached reader. Never blocks, never awaits.

        Synchronous on purpose: it is called from the read loop, and a publish
        that could suspend would let a slow consumer reorder the mutation
        stream against a later frame.
        """
        if self._closed:
            return
        self._buffer.append(value)
        if self._maxsize and len(self._buffer) > self._maxsize:
            dropped = len(self._buffer) - self._maxsize
            for _ in range(dropped):
                self._buffer.popleft()
            self._base += dropped
            # Fast-forward anyone standing in the hole. Leaving them behind
            # `_base` would make `_position` negative and yield a stale value.
            for key, cursor in self._cursors.items():
                if cursor < self._base:
                    self._cursors[key] = self._base
            if self._on_drop is not None:
                self._on_drop(dropped)
        self._wake_all()

    def close(self) -> None:
        """End every reader once it has drained what it has already been sent."""
        if self._closed:
            return
        self._closed = True
        self._wake_all()

    def reader(self) -> BroadcastReader[T]:
        """A fresh, independent cursor positioned at the current end."""
        return BroadcastReader(self)

    # ── internals used by BroadcastReader ────────────────────────────────────

    def _attach(self, key: int) -> None:
        self._cursors[key] = self._base + len(self._buffer)

    def _detach(self, key: int) -> None:
        self._cursors.pop(key, None)
        waiter = self._waiters.pop(key, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
        self._trim()

    def _has_value(self, key: int) -> bool:
        cursor = self._cursors.get(key)
        return cursor is not None and cursor < self._base + len(self._buffer)

    def _take(self, key: int) -> T:
        cursor = self._cursors[key]
        value = self._buffer[cursor - self._base]
        self._cursors[key] = cursor + 1
        self._trim()
        return value

    def _wait(self, key: int) -> asyncio.Future[None]:
        waiter = self._waiters.get(key)
        if waiter is None or waiter.done():
            waiter = asyncio.get_running_loop().create_future()
            self._waiters[key] = waiter
        return waiter

    def _wake_all(self) -> None:
        for waiter in self._waiters.values():
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

    def _trim(self) -> None:
        """Reclaim entries every cursor has passed.

        With no cursors left there is nothing to replay to, so the whole buffer
        goes -- otherwise a closed-but-referenced queue pins every value it ever
        carried.
        """
        if not self._cursors:
            self._base += len(self._buffer)
            self._buffer.clear()
            return
        lowest = min(self._cursors.values())
        drop = lowest - self._base
        if drop > 0:
            for _ in range(drop):
                self._buffer.popleft()
            self._base += drop


class BroadcastReader(Generic[T]):
    """One consumer's cursor over a :class:`BroadcastQueue`.

    Async-iterable, and terminal once closed: a reader that has been closed
    stays closed even if the queue still holds unread values.
    """

    __slots__ = ("_done", "_key", "_queue")

    def __init__(self, queue: BroadcastQueue[T]) -> None:
        self._queue = queue
        self._key = id(self)
        self._done = False
        queue._attach(self._key)

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        while True:
            if self._done:
                raise StopAsyncIteration
            if self._queue._has_value(self._key):
                return self._queue._take(self._key)
            if self._queue.closed:
                await self.aclose()
                raise StopAsyncIteration
            await self._queue._wait(self._key)

    async def aclose(self) -> None:
        """Detach. Idempotent.

        Deregistration is explicit rather than left to ``weakref.finalize``: a
        finaliser runs at arbitrary GC time, on whatever thread triggered the
        collection, possibly after the loop has closed -- and touching a
        ``Future`` from there is unsafe. ``async with aclosing(...)`` or an
        ``async for`` that runs to completion is the supported shape.
        """
        if self._done:
            return
        self._done = True
        self._queue._detach(self._key)
