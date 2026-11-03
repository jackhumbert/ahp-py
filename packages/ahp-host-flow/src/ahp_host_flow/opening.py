"""Marks the span in which this provider is opening a worker chat.

The host tells an agent a chat's complete config values the moment it exists --
for a worker chat (`SessionPublisher.open_tool_chat`) too, from inside that very
call, as the values it was seeded with from its parent. Those are the baseline the
worker's inner session was already built from (`inner_config`), not a change a
client made: forwarding them on top as an overlay would let the seed override the
per-call `effort` or approval mode the flow decided. So `chat_config_changed`
ignores a delivery that arrives inside an `opening_worker()` block.

The mark is the task that opened the chat (a context variable holding it), not a
flag: the host delivers in the task awaiting `open_tool_chat`, while a client's
`chat/configChanged` (which must still count) arrives in another task. A task
created inside the window copies the context, so a plain boolean would stay set
for its whole life; comparing against the current task does not. The new chat's
URI does not exist until the host makes it, so `seeds_chat` also requires that the
chat is not one the provider already holds.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection, Iterator
from contextvars import ContextVar

__all__ = ["is_opening_worker", "opening_worker", "seeds_chat"]

_OPENING: ContextVar[asyncio.Task[object] | None] = ContextVar(
    "ahp_host_flow_opening_worker", default=None
)


def _current() -> asyncio.Task[object] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:  # no running loop
        return None


@contextlib.contextmanager
def opening_worker() -> Iterator[None]:
    """Wrap an ``await publisher.open_tool_chat(...)``."""
    token = _OPENING.set(_current())
    try:
        yield
    finally:
        _OPENING.reset(token)


def is_opening_worker() -> bool:
    """Whether the current task is the one inside an `opening_worker()` block."""
    opener = _OPENING.get()
    return opener is not None and opener is _current()


def seeds_chat(chat: str, known: Collection[str]) -> bool:
    """Whether a delivery for *chat* is the host's seed for the chat being opened."""
    return is_opening_worker() and chat not in known
