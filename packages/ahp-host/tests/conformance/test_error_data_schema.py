"""Structured error `data` payloads, validated against the vendored schemas.

`test_wire_schema.py` validates actions, state and results. Errors were the
remaining hole, and the protocol declares exactly three payloads
(`AhpErrorDetailsMap`) -- one of which the host got wrong for its whole life:
-32005 carried `supportedProtocolVersions`, so the one frame that exists to
tell a user which host version to install was read as `undefined` by every
client that follows the schema.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider

from .schemas import validate_against

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _initialize(host: Host, params: dict[str, Any]) -> dict[str, Any]:
    client, server = memory_pair()
    serve = asyncio.create_task(host.serve(server))
    try:
        await client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
        message = await asyncio.wait_for(client.receive(), timeout=5)
        assert message is not None
        return dict(message)
    finally:
        serve.cancel()


class TestUnsupportedProtocolVersion:
    async def test_the_data_matches_the_declared_payload(self) -> None:
        """`UnsupportedProtocolVersionErrorData` requires `supportedVersions`.

        errors.schema.json:65,74 and vendor/upstream/ts/errors.ts:157. This is
        the assertion that fails on the misspelling; a client reading the wrong
        key cannot tell the user anything beyond "handshake failed", and cannot
        even tell that the failure was a version mismatch.
        """
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            response = await _initialize(
                host,
                {"channel": ROOT_URI, "clientId": "c", "protocolVersions": ["0.5.0"]},
            )
        finally:
            await host.aclose()

        assert response["error"]["code"] == -32005
        problems = validate_against(
            "errors", "UnsupportedProtocolVersionErrorData", response["error"]["data"]
        )
        assert not problems, "\n  ".join(problems)
        assert response["error"]["data"]["supportedVersions"] == list(host.supported_versions)
