"""An event loop whose clock is virtual: timers fire in order with no real waiting.

Not a test module. ``select(timeout)`` polls for real I/O first; when nothing is ready and the loop
would sleep for ``timeout``, the virtual clock jumps forward by that much instead, so a 600 s
scenario runs in milliseconds and the order of its timers is exact.
"""

from __future__ import annotations

import asyncio
import selectors
from typing import Any

__all__ = ["VirtualClockLoop"]


class _VirtualSelector(selectors.BaseSelector):
    """The real selector, except that a positive idle wait advances the loop's clock."""

    def __init__(self, loop: VirtualClockLoop) -> None:
        self._real = selectors.DefaultSelector()
        self._loop = loop

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self._real.unregister(fileobj)

    def modify(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.modify(fileobj, events, data)

    def get_map(self) -> Any:
        return self._real.get_map()

    def close(self) -> None:
        self._real.close()

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = self._real.select(0)
        if ready:
            return ready
        if timeout is None:
            # Nothing scheduled: only real I/O can wake the loop.
            return self._real.select(None)
        if timeout > 0:
            self._loop.advance(timeout)
        return []


class VirtualClockLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self._now = 0.0
        super().__init__(_VirtualSelector(self))

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def time(self) -> float:
        return self._now
