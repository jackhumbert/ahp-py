"""`meta.x.args` is checked at catalog time and published as `argsSchema`."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonschema
from ahp_protocol.ext.flow import DESCRIPTOR_SCHEMA, FLOW_META_KEY

from ahp_host_flow import catalog
from ahp_host_flow.engine import args_error

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
}


def _marker(meta: Mapping[str, Any] | None, tmp_path: Path) -> dict[str, Any]:
    entry = catalog.WorkflowEntry("w", tmp_path / "w.js", "user", catalog.USER_ID, meta, None)
    dirs = [catalog.SourceDir("user", tmp_path, catalog.USER_ID, "Workflows (user)")]
    (container,) = catalog.customizations([entry], dirs, version="0")
    (child,) = container["children"]
    marker: dict[str, Any] = child["_meta"][FLOW_META_KEY]
    return marker


def test_typedargs_catalog_valid_schema_is_published(tmp_path: Path) -> None:
    marker = _marker({"x": {"args": _SCHEMA}}, tmp_path)
    assert marker["argsSchema"] == _SCHEMA
    assert "warnings" not in marker
    jsonschema.Draft202012Validator(DESCRIPTOR_SCHEMA).validate(marker)


def test_typedargs_catalog_bad_schemas_warn(tmp_path: Path) -> None:
    for bad in ("string", True):
        marker = _marker({"x": {"args": bad}}, tmp_path)
        assert "argsSchema" not in marker
        assert marker["warnings"] == ["meta.x.args must be a JSON Schema object"]

    marker = _marker({"x": {"args": {"type": 5}}}, tmp_path)
    assert "argsSchema" not in marker
    (warning,) = marker["warnings"]
    assert warning.startswith("meta.x.args is not a valid JSON Schema:")

    marker = _marker({"x": {"args": {"$ref": "https://example.com/s.json"}}}, tmp_path)
    assert marker["warnings"] == [
        "meta.x.args may only reference itself ('#...'), not 'https://example.com/s.json'"
    ]


def test_typedargs_catalog_schedule_warning_comes_first(tmp_path: Path) -> None:
    marker = _marker({"x": {"schedule": 5, "args": "string"}}, tmp_path)
    assert marker["warnings"] == [
        "meta.x.schedule must be a cron string or {expression, timeZone?}",
        "meta.x.args must be a JSON Schema object",
    ]


def test_typedargs_catalog_no_args() -> None:
    assert catalog.args_schema_of(None) == (None, None)
    assert catalog.args_schema_of({"x": "y"}) == (None, None)
    assert catalog.args_schema_of({"x": {}}) == (None, None)


def test_typedargs_catalog_args_error() -> None:
    assert args_error(_SCHEMA, {"n": "x"}) == (
        "args do not match meta.x.args: n: 'x' is not of type 'integer'"
    )
    assert args_error(_SCHEMA, {}) == (
        "args do not match meta.x.args: (root): 'n' is a required property"
    )
    assert args_error(_SCHEMA, {"n": 2}) is None


async def test_typedargs_catalog_python_workflow(tmp_path: Path) -> None:
    (tmp_path / "typed.py").write_text(
        "meta = {'name': 'typed', 'description': 'd', 'x': {'args': {'type': 'object'}}}\n",
        encoding="utf-8",
    )
    dirs = [catalog.SourceDir("user", tmp_path, catalog.USER_ID, "Workflows (user)")]
    entries = await catalog.discover(dirs, node=None)
    (container,) = catalog.customizations(entries, dirs, version="0")
    (child,) = container["children"]
    assert child["_meta"][FLOW_META_KEY]["argsSchema"] == {"type": "object"}
