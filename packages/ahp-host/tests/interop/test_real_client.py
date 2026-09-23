"""Interop: the real Microsoft TypeScript client against our host, over a socket.

Skipped unless Node and the published client are available:

    npm i --no-save @microsoft/agent-host-protocol@0.8.0 ws

This is the only test in the suite that needs anything outside Python. It is
worth the cost: the reference client validates almost nothing, so nothing else
will tell us we have drifted -- but a client that cannot complete a handshake or
render a turn is a failure no fixture corpus would catch either.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.provider import EchoProvider
from agent_host_server.ws import serve_websocket

pytestmark = [pytest.mark.interop, pytest.mark.anyio]

ROOT = Path(__file__).resolve().parents[2]
DRIVER = Path(__file__).parent / "driver.mjs"


SETUP = "needs node + `npm i --no-save @microsoft/agent-host-protocol@0.8.0 ws`"


def _client_available() -> bool:
    if shutil.which("node") is None:
        return False
    return (ROOT / "node_modules" / "@microsoft" / "agent-host-protocol").is_dir()


if os.environ.get("AHP_INTEROP_REQUIRED") and not _client_available():
    # CI installs the client, so a skip there means the *setup* broke -- and a
    # skipped interop test is indistinguishable from a green one at a glance.
    # This is the only independent check we have; it may not go quiet.
    raise RuntimeError(f"AHP_INTEROP_REQUIRED is set but the client is missing: {SETUP}")


requires_client = pytest.mark.skipif(not _client_available(), reason=SETUP)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@requires_client
async def test_real_client_drives_a_turn_end_to_end() -> None:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    async with serve_websocket(host, connection_token="interop-token") as server:
        url = server.url
        process = await asyncio.create_subprocess_exec(
            "node",
            str(DRIVER),
            url,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    await host.aclose()

    assert stdout, f"driver produced no output; stderr:\n{stderr.decode()}"
    report = json.loads(stdout.decode())
    assert report["ok"], f"driver failed: {report['errors']}\nstderr:\n{stderr.decode()}"

    # The handshake.
    # The 0.8.0 client offers 0.8.0 first, and this host now covers it.
    assert report["negotiatedVersion"] == "0.8.0"
    assert report["snapshotsIsArray"] is True
    assert report["rootSnapshotResource"] == "ahp-root://"
    assert report["agents"] == ["echo"]
    assert report["listSessionsHasItems"] is True

    # Session bring-up published a chat the client could find and subscribe to.
    assert report["chatUri"], "the client could not discover a chat"

    # Ordering.
    assert report["seqsAreMonotonic"] is True, report["chatActionSeqs"]

    # The turn actually ran, through the echo provider.
    turns = report["turns"]
    assert len(turns) == 1, turns
    assert turns[0]["state"] == "complete"
    assert "hello from the interop driver" in turns[0]["text"]

    assert report["pingOk"] is True


@requires_client
async def test_official_reducers_agree_with_our_state() -> None:
    """Feed our action stream through the OFFICIAL reducers and diff.

    Fixture conformance proves equivalence over recorded inputs; this proves it
    over live traffic our host actually generated.
    """
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    async with serve_websocket(host) as server:
        process = await asyncio.create_subprocess_exec(
            "node",
            str(DRIVER),
            server.url,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    await host.aclose()

    report = json.loads(stdout.decode())
    assert report["ok"], f"{report['errors']}\n{stderr.decode()}"

    # `modifiedAt` is EXPECTED to differ and is not a defect: the chat reducer
    # reads the wall clock in six places, so the host and the client each stamp
    # their own value for the same action. The protocol calls its reducers
    # "pure" and requires them to "run identically on host and client"; that is
    # factually not achievable for this field, which is why every language port
    # injects a clock and the fixture corpus pins it to 9999.
    #
    # Every OTHER key must agree exactly.
    assert report["reducerAgreementIgnoringClock"] is True, (
        "our authoritative state diverged from the official TypeScript reducers "
        f"on {report['divergentKeys']}\n"
        f"  ours:     {json.dumps(report['hostState'], sort_keys=True)}\n"
        f"  official: {json.dumps(report['mirroredState'], sort_keys=True)}"
    )
    assert report["divergentKeys"] in ([], ["modifiedAt"]), report["divergentKeys"]


async def test_wrong_connection_token_is_rejected_with_403() -> None:
    """A bad token must be refused with HTTP 403 during the upgrade.

    Asserted at the HTTP level on purpose. An earlier version only checked that
    the client failed to connect, which passed for the wrong reason: the
    handshake hook was raising (`respond` is on the connection, not the
    request), so *every* connection was aborted and the test still went green.
    """
    import urllib.error
    import urllib.request

    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    async with serve_websocket(host, connection_token="the-real-token") as server:
        port = server.bound_port

        def fetch(url: str) -> int:
            request = urllib.request.Request(
                url,
                headers={
                    "Upgrade": "websocket",
                    "Connection": "Upgrade",
                    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                    "Sec-WebSocket-Version": "13",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return int(response.status)
            except urllib.error.HTTPError as exc:
                return int(exc.code)

        assert await asyncio.to_thread(fetch, f"http://127.0.0.1:{port}/?tkn=nope") == 403
        assert await asyncio.to_thread(fetch, f"http://127.0.0.1:{port}/") == 403
    await host.aclose()


@requires_client
async def test_wrong_connection_token_stops_the_real_client() -> None:
    """And the real client cannot get through it either."""
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    async with serve_websocket(host, connection_token="the-real-token") as server:
        bad_url = server.url.replace("the-real-token", "not-the-token")
        process = await asyncio.create_subprocess_exec(
            "node",
            str(DRIVER),
            bad_url,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    await host.aclose()

    report = json.loads(stdout.decode())
    assert report["ok"] is False, "a bad connection token must not connect"
