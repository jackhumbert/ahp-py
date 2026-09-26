"""Interop: the Python AHP client against our host, in-process.

The third genuinely independent check, and the widest. `tests/conformance`
proves our reducers agree with upstream's over recorded inputs;
`test_real_client.py` proves the published TypeScript client can drive us. This
proves a client built independently from the same spec, by people who read it
separately, agrees with what we PUT ON THE WIRE.

Reducer agreement is deliberately NOT the evidence here -- both peers import
`ahp_protocol`, so the reducers are literally the same code. What is
independent is every command shape, every published action, and the two
directions of the `resource*` family.

Unlike the TypeScript driver this needs no subprocess and no socket: both ends
take a `Transport`, so they meet over `memory_pair()`. That makes the suite fast
and deterministic, and lets an assertion compare the client's mirrored state
against `host.sequencer.state_of(...)` directly -- something the out-of-process
driver can only do by serialising a report.

Skipped unless the client is installed:

    pip install -e ../ahp-client
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider

pytestmark = [pytest.mark.interop, pytest.mark.anyio]

SETUP = "needs the sibling client: pip install -e ../ahp-client"
_AVAILABLE = importlib.util.find_spec("ahp_client") is not None

if os.environ.get("AHP_INTEROP_REQUIRED") and not _AVAILABLE:
    # Same rule as the TypeScript interop suite: in CI the client IS installed,
    # so a skip means the setup broke -- and a skipped interop suite is
    # indistinguishable from a passing one at a glance.
    raise RuntimeError(f"AHP_INTEROP_REQUIRED is set but the client is missing: {SETUP}")

requires_client = pytest.mark.skipif(not _AVAILABLE, reason=SETUP)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = Host(EchoProvider(), LoopbackSingleUserPolicy())
    try:
        yield made
    finally:
        await made.aclose()


class _Paired:
    """A host and a connected client, torn down together."""

    def __init__(self, host: Host) -> None:
        self.host = host
        self.client: Any = None
        self._serve: asyncio.Task[None] | None = None
        self._ctx: Any = None

    async def __aenter__(self) -> Any:
        from ahp_client import connect

        client_transport, server_transport = memory_pair()
        self._serve = asyncio.create_task(self.host.serve(server_transport))
        self._ctx = connect(transport=client_transport)
        # Bounded on purpose. `connect()` parks forever on a permanently
        # refused handshake (a client-side defect this suite found), and an
        # un-timeouted await would turn that into a hung run rather than a
        # failure.
        self.client = await asyncio.wait_for(self._ctx.__aenter__(), timeout=20)
        return self.client

    async def __aexit__(self, *exc: Any) -> None:
        if self._ctx is not None:
            # Best-effort: a teardown that raises would mask the assertion that
            # actually failed, and a teardown that hangs would wedge the run.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._ctx.__aexit__(None, None, None), timeout=10)
        if self._serve is not None:
            self._serve.cancel()


@requires_client
async def test_the_clients_conformance_probe_passes(host: Host) -> None:
    """`doctor` is the client's own MUST/SHOULD checklist, each item citing the
    line of spec it comes from. It exists to turn adoption into conformance
    data, which makes it the closest thing to an external audit this project
    has -- so a regression here is a bug report someone else would have filed.
    """
    from ahp_client.doctor import diagnose

    client_transport, server_transport = memory_pair()
    serve = asyncio.create_task(host.serve(server_transport))
    try:
        report = await asyncio.wait_for(diagnose(client_transport), timeout=30)
    finally:
        serve.cancel()

    assert report.ok, "\n".join(str(f) for f in report.failures)
    # Not a bare `ok`: an empty report is also "ok", and a probe that silently
    # stopped checking would then read as a pass.
    assert len(report.findings) >= 10, str(report)


@requires_client
async def test_a_turn_streams_through_the_high_level_api(host: Host) -> None:
    """The whole point: an independently built client drives a turn end to end
    and reassembles exactly the text we sent."""
    from ahp_client import Delta, TurnCompleted

    async with (
        _Paired(host) as client,
        await asyncio.wait_for(client.create_session(provider="echo"), timeout=20) as session,
    ):
        text = ""
        completed = False
        async for event in session.prompt("hello from the python client"):
            if isinstance(event, Delta):
                text += event.text
            elif isinstance(event, TurnCompleted):
                completed = True
                break

    assert completed, "the turn never settled"
    assert "hello from the python client" in text


@requires_client
async def test_the_clients_mirror_matches_our_state(host: Host) -> None:
    """Both peers run the SAME reducers, so any difference here is a delivery
    or ordering defect rather than a reducer one -- which is exactly what makes
    it worth asserting. The reducers are covered by the fixture corpus; the
    wire between them is covered by nothing else.
    """
    from ahp_client import TurnCompleted

    async with (
        _Paired(host) as client,
        await asyncio.wait_for(client.create_session(provider="echo"), timeout=20) as session,
    ):
        async for event in session.prompt("mirror me"):
            if isinstance(event, TurnCompleted):
                break
        await asyncio.sleep(0.3)

        chat = await session.chat()
        chat_uri = chat.state["resource"]
        mirrored = dict(chat.state)
        # Read INSIDE the session context. Leaving it disposes the session,
        # which drops the channel -- so the comparison would be against None.
        host_state = host.sequencer.state_of(chat_uri)
        assert isinstance(host_state, dict), "the host dropped the chat channel"
        ours = dict(host_state)

    # Field by field, not just the turn ids: the interesting failure is a
    # delivered-but-misapplied envelope, which a shallow comparison hides.
    # `modifiedAt` is excluded ONLY because each side stamps it from its own
    # clock -- everything else is the host's bytes and must survive the trip.
    ignored = {"modifiedAt"}
    differing = {
        key
        for key in set(mirrored) | set(ours)
        if key not in ignored and mirrored.get(key) != ours.get(key)
    }
    assert not differing, f"the mirror diverged on {sorted(differing)}"
