"""Workflow discovery, precedence and customizations."""

from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host_flow.catalog import (
    FLOW_META_KEY,
    PLUGIN_ID,
    USER_ID,
    SourceDir,
    WorkflowEntry,
    customizations,
    directory_of,
    discover,
    resolve,
    source_dirs,
    visible_names,
)


def _tree(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    wd = tmp_path / "proj"
    ws = wd / ".claude" / "workflows"
    user = tmp_path / "user"
    host1 = tmp_path / "host1"
    host2 = tmp_path / "host2"
    for directory, names in ((ws, "ab"), (user, "ac"), (host1, "cd"), (host2, "d")):
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / f"{name}.js").write_text("return 1\n", encoding="utf-8")
    return wd, user, [host1, host2]


def _dirs(
    tmp_path: Path, sources: tuple[str, ...] = ("workspace", "user", "host")
) -> list[SourceDir]:
    wd, user, hosts = _tree(tmp_path)
    return source_dirs(working_directories=[wd], user_dir=user, host_dirs=hosts, sources=sources)


def _source(entry: WorkflowEntry | None) -> str:
    assert entry is not None
    return entry.source


def test_source_dirs_order_and_filtering(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    assert [(d.source, d.container_id) for d in dirs] == [
        ("workspace", "ahp-host-flow.workspace.0"),
        ("user", USER_ID),
        ("host", PLUGIN_ID),
        ("host", PLUGIN_ID),
    ]
    assert dirs[0].path == tmp_path / "proj" / ".claude" / "workflows"
    assert dirs[0].label == "Workflows (proj)"
    assert dirs[1].label == "Workflows (user)"
    assert dirs[2].label == "ahp-host-flow"
    assert [d.source for d in _dirs(tmp_path, ("workspace", "host"))] == [
        "workspace",
        "host",
        "host",
    ]
    assert [
        d.source
        for d in source_dirs(
            working_directories=[tmp_path / "proj"],
            user_dir=None,
            host_dirs=[],
            sources=("workspace", "user"),
        )
    ] == ["workspace"]


async def test_discover_precedence(tmp_path: Path) -> None:
    entries = await discover(_dirs(tmp_path), node=None)
    assert [(e.source, e.name, e.shadowed_by) for e in entries] == [
        ("workspace", "a", None),
        ("workspace", "b", None),
        ("user", "a", "workspace"),
        ("user", "c", None),
        ("host", "c", "user"),
        ("host", "d", None),
        ("host", "d", "host"),
    ]
    assert all(e.meta is None for e in entries)
    assert visible_names(entries) == ["a", "b", "c", "d"]


async def test_resolve(tmp_path: Path) -> None:
    entries = await discover(_dirs(tmp_path), node=None)
    assert _source(resolve(entries, "a")) == "workspace"
    assert _source(resolve(entries, "user:a")) == "user"
    assert _source(resolve(entries, "host:c")) == "host"
    assert resolve(entries, "zzz") is None
    assert resolve(entries, "user:b") is None


def test_directory_of(tmp_path: Path) -> None:
    assert directory_of(tmp_path.as_uri()) == tmp_path
    assert directory_of(str(tmp_path)) == tmp_path
    assert directory_of("vscode-remote://x/y") is None
    assert directory_of("file://otherhost/y") is None


async def test_customizations(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    entries = await discover(dirs, node=None)
    containers = customizations(entries, dirs, version="1.2.3")
    assert [c["id"] for c in containers] == ["ahp-host-flow.workspace.0", USER_ID, PLUGIN_ID]
    user = containers[1]
    assert user["type"] == "directory"
    assert user["contents"] == "prompt"
    assert user["writable"] is False
    assert user["enabled"] is True
    assert user["name"] == "Workflows (user)"
    assert user["uri"] == (tmp_path / "user").as_uri()
    flow = {"kind": "workflow", "phases": [], "source": "user", "shadowedBy": "workspace"}
    assert user["children"][0] == {
        "type": "prompt",
        "id": f"{USER_ID}.a",
        "uri": (tmp_path / "user" / "a.js").as_uri(),
        "name": "a",
        "description": "",
        "enabled": True,
        "_meta": {FLOW_META_KEY: flow},
    }
    plugin = containers[2]
    assert plugin["type"] == "plugin"
    assert "enabled" not in plugin
    assert plugin["version"] == "1.2.3"
    assert plugin["uri"] == (tmp_path / "host1").as_uri()
    assert [c["name"] for c in plugin["children"]] == ["c", "d"]


async def test_customizations_skip_missing_directories(tmp_path: Path) -> None:
    dirs = [SourceDir("user", tmp_path / "nope", USER_ID, "Workflows (user)")]
    assert customizations(await discover(dirs, node=None), dirs, version="1") == []


@pytest.mark.node
async def test_meta_is_read_not_executed(tmp_path: Path, node_path: str) -> None:
    directory = tmp_path / "wf"
    directory.mkdir()
    (directory / "a.js").write_text(
        "export const meta = { name: 'a', description: 'desc', whenToUse: 'when',"
        " phases: [{ title: 'P1' }] }\nthrow new Error('ran')",
        encoding="utf-8",
    )
    (directory / "b.js").write_text("export const meta = { name: f() }\n", encoding="utf-8")
    dirs = [SourceDir("user", directory, USER_ID, "Workflows (user)")]
    entries = await discover(dirs, node=node_path)
    by_name = {e.name: e for e in entries}
    assert by_name["b"].meta is None
    assert by_name["a"].meta is not None
    child = customizations(entries, dirs, version="1")[0]["children"][0]
    assert child["description"] == "when"
    assert child["_meta"][FLOW_META_KEY]["phases"] == [{"title": "P1"}]
