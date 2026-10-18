"""ahp-py's delegation origin, client side: `create_session(origin=)`."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from ahp_protocol.ext.delegation import delegation_origin

from ._sibling import requires_sibling_host

ORIGIN = delegation_origin("flow:/run-1", "ahp-chat:/w", "agent-0")


@requires_sibling_host
async def test_delegation_client_sends_the_origin() -> None:
    from ahp_host.core import Host, LoopbackSingleUserPolicy
    from ahp_host.provider import EchoProvider
    from ahp_protocol.transport import memory_pair

    from ahp_client.api import connect

    host = Host(provider=EchoProvider(), policy=LoopbackSingleUserPolicy())
    client_side, host_side = memory_pair()
    served = asyncio.get_running_loop().create_task(host.serve(host_side))
    try:
        async with connect(transport=client_side) as client:
            provider = str(client.agents()[0]["provider"])
            session = await client.create_session(provider=provider, cwd=".", origin=ORIGIN)
            state = host.sequencer.state_of(session.uri)
            assert state["origin"] == ORIGIN
            listed = await client.sessions()
            (entry,) = [i for i in listed["items"] if i["resource"] == session.uri]
            assert entry["origin"] == ORIGIN

            with pytest.raises(ValueError, match="origin must be a delegation origin"):
                await client.create_session(
                    provider=provider, cwd=".", origin={"kind": "automation"}
                )
    finally:
        served.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await served
