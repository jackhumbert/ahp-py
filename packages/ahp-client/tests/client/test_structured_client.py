"""ahp-py's structured-output extension, client side: `output_schema=` and
`TurnCompleted.structured`."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ahp_protocol.ext.structured import STRUCTURED_OUTPUT_ERROR_TYPE

from ahp_client.client import actions

from ._sibling import requires_sibling_host

OBJ: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
}
ARR: dict[str, Any] = {"type": "array", "items": {"type": "integer"}}


def test_turn_started_writes_meta_only_when_given() -> None:
    assert actions.turn_started("t", text="x", meta={"k": 1})["message"]["_meta"] == {"k": 1}
    assert "_meta" not in actions.turn_started("t", text="x")["message"]


async def _run(scenario: Any) -> None:
    from ahp_host.core import Host, LoopbackSingleUserPolicy
    from ahp_host.provider import EchoProvider
    from ahp_protocol.transport import memory_pair

    from ahp_client.api import connect

    host = Host(provider=EchoProvider(structured=True), policy=LoopbackSingleUserPolicy())
    client_side, host_side = memory_pair()
    served = asyncio.get_running_loop().create_task(host.serve(host_side))
    try:
        async with connect(transport=client_side) as client:
            provider = str(client.agents()[0]["provider"])
            session = await client.create_session(provider=provider, cwd=".")
            await scenario(session)
    finally:
        served.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await served


@requires_sibling_host
async def test_an_object_result_is_decoded() -> None:
    from ahp_client.api import TurnCompleted

    async def scenario(session: Any) -> None:
        chat = await session.chat()
        result = await chat.prompt('{"answer": 42}', output_schema=OBJ, idle_timeout=10.0)
        assert isinstance(result, TurnCompleted)
        assert result.structured == {"answer": 42}

    await _run(scenario)


@requires_sibling_host
async def test_an_array_result_is_wrapped_by_the_host_and_unwrapped_here() -> None:
    from ahp_client.api import TurnCompleted

    async def scenario(session: Any) -> None:
        chat = await session.chat()
        result = await chat.prompt("[1, 2, 3]", output_schema=ARR, idle_timeout=10.0)
        assert isinstance(result, TurnCompleted)
        assert result.structured == [1, 2, 3]

    await _run(scenario)


@requires_sibling_host
async def test_no_valid_result_fails_the_turn_with_the_extension_error() -> None:
    from ahp_client.api import TurnFailed

    async def scenario(session: Any) -> None:
        chat = await session.chat()
        result = await chat.prompt("nope", output_schema=OBJ, idle_timeout=10.0)
        assert isinstance(result, TurnFailed)
        assert result.error_type == STRUCTURED_OUTPUT_ERROR_TYPE

    await _run(scenario)


@requires_sibling_host
async def test_an_ordinary_turn_has_no_structured_value() -> None:
    from ahp_client.api import TurnCompleted

    async def scenario(session: Any) -> None:
        chat = await session.chat()
        result = await chat.prompt("hello", idle_timeout=10.0)
        assert isinstance(result, TurnCompleted)
        assert result.structured is None

    await _run(scenario)


@requires_sibling_host
async def test_the_lazy_session_path_yields_structured() -> None:
    from ahp_client.api import TurnCompleted

    async def scenario(session: Any) -> None:
        result = await session.prompt('{"answer": 42}', output_schema=OBJ, idle_timeout=10.0)
        assert isinstance(result, TurnCompleted)
        assert result.structured == {"answer": 42}

    await _run(scenario)
