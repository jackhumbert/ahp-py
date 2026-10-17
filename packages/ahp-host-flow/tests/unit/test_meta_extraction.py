"""Reading `meta` from a script with acorn, without running it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ahp_host_flow.sandbox import MetaError, Sandbox, ScriptSyntaxError, extract_meta

pytestmark = pytest.mark.node


async def _meta(source: str, node: str) -> dict[str, Any] | None:
    return await asyncio.wait_for(extract_meta(source, node=node), 30)


async def test_meta_extraction_reads_a_literal(node_path: str) -> None:
    source = """
export const meta = {
  name: 'fanout',
  description: "Run one instruction over a list",
  whenToUse: `Bulk work`,
  phases: [{title: 'Map', detail: 'one agent per item', model: 'haiku'}],
  retries: -1,
  'quoted-key': true,
  x: {schedule: '0 * * * *'},
}
return 1
"""
    assert await _meta(source, node_path) == {
        "name": "fanout",
        "description": "Run one instruction over a list",
        "whenToUse": "Bulk work",
        "phases": [{"title": "Map", "detail": "one agent per item", "model": "haiku"}],
        "retries": -1,
        "quoted-key": True,
        "x": {"schedule": "0 * * * *"},
    }


async def test_meta_extraction_without_meta(node_path: str) -> None:
    assert await _meta("return 1", node_path) is None


async def test_meta_extraction_never_runs_the_script(node_path: str) -> None:
    source = "export const meta = {name: 'x', description: 'y'}\nthrow new Error('ran')"
    assert await _meta(source, node_path) == {"name": "x", "description": "y"}


@pytest.mark.parametrize(
    "source",
    [
        "export const meta = {name: Date.now()}",
        "export const meta = {name: `a${x}`}",
        "export const meta = {[k]: 1}",
        "export const meta = {name}",
        "export const meta = {...o}",
        "export const meta = {f() {}}",
        "export const meta = {a: [1,,2]}",
        "export const meta = {a: foo}",
        "export const meta = {a: /a/}",
        "export const meta = 5",
    ],
)
async def test_meta_extraction_rejects_non_literals(source: str, node_path: str) -> None:
    with pytest.raises(MetaError) as info:
        await _meta(source, node_path)
    if "Date.now()" in source:
        assert "meta." in str(info.value)


@pytest.mark.parametrize(
    "source",
    [
        "import x from 'y'",
        "export default 1",
        "export let meta = {}",
        "export const other = 1",
        "export const meta = {}\nexport const meta = {}",
    ],
)
async def test_meta_extraction_rejects_module_syntax(source: str, node_path: str) -> None:
    with pytest.raises(ScriptSyntaxError) as info:
        await _meta(source, node_path)
    assert "import/export is not allowed" in str(info.value)


async def test_meta_extraction_reports_parse_errors(node_path: str) -> None:
    with pytest.raises(ScriptSyntaxError) as info:
        await _meta("const = ;", node_path)
    assert "(1:" in str(info.value)


async def test_meta_extraction_reuses_one_sandbox(node_path: str) -> None:
    async with Sandbox(node_path) as sandbox:
        first = await asyncio.wait_for(sandbox.meta("export const meta = {a: 1}"), 30)
        second = await asyncio.wait_for(sandbox.meta("return 1"), 30)
    assert first == {"a": 1}
    assert second is None
