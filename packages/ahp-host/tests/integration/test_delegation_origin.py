"""A client can name the delegation a session was started for (`docs/requests.md` entry 12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.ext.delegation import ORIGIN_META_KEY, delegation_origin

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider
from tests.unit.hosting import connect, open_session, shut, state

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


ORIGIN = delegation_origin("flow:/run-1", "ahp-chat:/w", "agent-0")


def _host(tmp_path: Path | None = None) -> Host:
    if tmp_path is None:
        return Host(EchoProvider(), LoopbackSingleUserPolicy())
    return Host(
        EchoProvider(),
        LoopbackSingleUserPolicy(),
        store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
        sequence_file=tmp_path / "seq",
    )


async def _listed(wire: Any, uri: str) -> dict[str, Any]:
    response = await wire.request("listSessions", {"channel": ROOT_URI})
    (entry,) = [item for item in response["result"]["items"] if item["resource"] == uri]
    return dict(entry)


async def test_delegation_origin_is_seeded_and_listed() -> None:
    host = _host()
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/d1", _meta={ORIGIN_META_KEY: ORIGIN})
        assert state(host, "echo:/d1")["origin"] == ORIGIN
        assert (await _listed(wire, "echo:/d1"))["origin"] == ORIGIN
    finally:
        await shut(host, wire, serving)


async def test_delegation_origin_survives_the_store(tmp_path: Path) -> None:
    host = _host(tmp_path)
    wire, serving = await connect(host)
    await open_session(wire, "echo:/d2", _meta={ORIGIN_META_KEY: ORIGIN})
    await shut(host, wire, serving)

    host = _host(tmp_path)
    try:
        assert await host.restore() == 1
        assert state(host, "echo:/d2")["origin"] == ORIGIN
    finally:
        await host.aclose()


@pytest.mark.parametrize(
    "claimed",
    [
        {"kind": "automation", "automation": "a", "trigger": "t"},
        {"kind": "x"},
    ],
)
async def test_delegation_origin_ignores_other_kinds(claimed: dict[str, Any]) -> None:
    host = _host()
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/d3", _meta={ORIGIN_META_KEY: claimed})
        assert "origin" not in state(host, "echo:/d3")
        assert "origin" not in await _listed(wire, "echo:/d3")
    finally:
        await shut(host, wire, serving)
