"""The host's seed for a worker chat is not a client's change (`ahp_host_flow.opening`)."""

from __future__ import annotations

import asyncio

from ahp_host_flow.opening import is_opening_worker, opening_worker, seeds_chat


def test_the_mark_is_scoped_to_the_block() -> None:
    async def scenario() -> list[bool]:
        before = is_opening_worker()
        with opening_worker():
            inside = is_opening_worker()
        return [before, inside, is_opening_worker()]

    assert asyncio.run(scenario()) == [False, True, False]


def test_a_task_started_outside_the_block_does_not_see_it() -> None:
    """The host delivers its seed in the task awaiting `open_tool_chat`; a client's
    `chat/configChanged` arrives in another task and must still count."""

    async def scenario() -> tuple[bool, bool]:
        async def probe() -> bool:
            await gate.wait()
            return is_opening_worker()

        gate = asyncio.Event()
        outside = asyncio.create_task(probe())
        with opening_worker():
            await asyncio.sleep(0)
            opener = is_opening_worker()
        gate.set()
        return await outside, opener

    outside, opener = asyncio.run(scenario())
    assert outside is False
    assert opener is True


def test_a_task_spawned_inside_the_block_does_not_inherit_the_mark() -> None:
    """A task created in the window copies the context; it must not keep the mark for life."""

    async def scenario() -> tuple[bool, bool]:
        async def probe() -> bool:
            await gate.wait()
            return is_opening_worker()

        gate = asyncio.Event()
        with opening_worker():
            spawned = asyncio.create_task(probe())
            here = is_opening_worker()
        gate.set()
        return here, await spawned

    here, spawned = asyncio.run(scenario())
    assert here is True
    assert spawned is False


def test_a_delivery_for_a_chat_already_known_is_honoured_inside_the_window() -> None:
    """Only the chat being opened is a seed. Its URI does not exist before the host makes it,
    so any chat the provider already holds is a different chat and its change counts."""
    async def scenario() -> list[str]:
        known = {"ahp-chat:/old"}
        with opening_worker():
            return [c for c in ("ahp-chat:/old", "ahp-chat:/new") if seeds_chat(c, known)]

    assert asyncio.run(scenario()) == ["ahp-chat:/new"]
