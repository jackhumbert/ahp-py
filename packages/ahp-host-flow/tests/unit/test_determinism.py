"""Non-deterministic APIs throw inside a script; explicit dates still work."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ahp_host_flow.engine import EchoDispatcher
from ahp_host_flow.sandbox import ScriptError, run_script

pytestmark = pytest.mark.node


async def _run(source: str, node: str) -> Any:
    return await asyncio.wait_for(run_script(source, node=node, dispatcher=EchoDispatcher()), 30)


@pytest.mark.parametrize(
    ("source", "api"),
    [
        ("return Date.now()", "Date.now()"),
        ("return Math.random()", "Math.random()"),
        ("return new Date()", "new Date()"),
        ("return Date()", "Date()"),
    ],
)
async def test_determinism_banned_api_throws(node_path: str, source: str, api: str) -> None:
    with pytest.raises(ScriptError) as exc:
        await _run(source, node_path)
    assert f"{api} is not allowed in a workflow script" in str(exc.value)


async def test_determinism_explicit_dates_work(node_path: str) -> None:
    source = (
        "return [new Date(0).toISOString(), Date.UTC(2020, 0, 1),"
        " Date.parse('1970-01-02T00:00:00Z'), new Date(0) instanceof Date,"
        " new Date(0).constructor === Date, Math.floor(2.5)]"
    )
    assert await _run(source, node_path) == [
        "1970-01-01T00:00:00.000Z",
        1577836800000,
        86400000,
        True,
        True,
        2,
    ]


async def test_determinism_catchable_in_script(node_path: str) -> None:
    source = "try { Date.now() } catch (e) { return e instanceof Error }"
    assert await _run(source, node_path) is True


@pytest.mark.parametrize(
    "source",
    [
        "return this.constructor.constructor('return Date')().now()",
        "return this.constructor.constructor('return Math')().random()",
        "return Function('return Date')().now()",
        "return new Date(0).constructor.now()",
        "return Object.getPrototypeOf(new Date(0)).constructor.now()",
        "return RealDate.now()",
    ],
)
async def test_determinism_no_route_to_real_apis(node_path: str, source: str) -> None:
    with pytest.raises(ScriptError):
        await _run(source, node_path)
