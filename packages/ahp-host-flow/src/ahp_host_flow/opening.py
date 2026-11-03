"""Marks the span in which this provider is opening a worker chat.

The host tells an agent a chat's complete config values the moment it exists --
for a worker chat (`SessionPublisher.open_tool_chat`) too, from inside that very
call, as the values it was seeded with from its parent. Those are the baseline the
worker's inner session was already built from (`inner_config`), not a change a
client made: forwarding them on top as an overlay would let the seed override the
per-call `effort` or approval mode the flow decided. So `chat_config_changed`
ignores a delivery that arrives inside an `opening_worker()` block.

A context variable rather than a flag on the provider: the host delivers in the
task that is awaiting `open_tool_chat`, while a client's `chat/configChanged`
(which must still count) arrives in another task, and parallel opens do not see
each other's mark.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

__all__ = ["is_opening_worker", "opening_worker"]

_OPENING: ContextVar[bool] = ContextVar("ahp_host_flow_opening_worker", default=False)


@contextlib.contextmanager
def opening_worker() -> Iterator[None]:
    """Wrap an ``await publisher.open_tool_chat(...)``."""
    token = _OPENING.set(True)
    try:
        yield
    finally:
        _OPENING.reset(token)


def is_opening_worker() -> bool:
    """Whether the current task is inside an `opening_worker()` block."""
    return _OPENING.get()
