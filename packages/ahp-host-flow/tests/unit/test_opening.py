"""The host's seed for a worker chat is not a client's change (`ahp_host_flow.opening`)."""

from __future__ import annotations

import asyncio

from ahp_host_flow.opening import is_opening_worker, opening_worker


def test_the_mark_is_scoped_to_the_block() -> None:
    assert not is_opening_worker()
    with opening_worker():
        assert is_opening_worker()
    assert not is_opening_worker()


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
            inside = asyncio.create_task(probe())
            await asyncio.sleep(0)
        gate.set()
        return await outside, await inside

    outside, inside = asyncio.run(scenario())
    assert outside is False
    assert inside is True
