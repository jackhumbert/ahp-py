"""Workflow discovery, precedence and customizations."""

from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host_flow.catalog import (
    CLIENT_PREFIX,
    FLOW_META_KEY,
    MAX_CLIENT_WORKFLOW_BYTES,
    MAX_CLIENT_WORKFLOWS,
    PLUGIN_ID,
    USER_ID,
    ClientPlugin,
    SourceDir,
    WorkflowEntry,
    client_plugins,
    customizations,
    directory_of,
    discover,
    python_dirs,
    read_client_workflows,
    resolve,
    schedule_of,
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


async def test_catalog_skips_symlinks_and_non_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "wf"
    directory.mkdir()
    (directory / "real.js").write_text("return 1\n", encoding="utf-8")
    (directory / "dir.js").mkdir()
    link = directory / "link.js"
    # Whatever the platform allows, the entry is a symlink as far as the catalog can tell, so the
    # assertion always runs (creating a real link needs a privilege some systems withhold).
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: self.name == "link.js" or real_is_symlink(self)
    )
    link.write_text("return 2\n", encoding="utf-8")
    dirs = [SourceDir("user", directory, USER_ID, "Workflows (user)")]
    names = [e.name for e in await discover(dirs, node=None)]
    assert "dir" not in names
    assert "real" in names
    assert "link" not in names


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


def _python_tree(tmp_path: Path) -> list[SourceDir]:
    dirs = {"workspace": "w", "user": "u", "host": "h"}
    result: list[SourceDir] = []
    for source, name in dirs.items():
        directory = tmp_path / source
        directory.mkdir()
        (directory / f"{name}.py").write_text(
            f"meta = {{'whenToUse': 'use {name}'}}\n", encoding="utf-8"
        )
        result.append(SourceDir(source, directory, f"id.{source}", source))
    return result


async def test_discover_python_default_sources(tmp_path: Path) -> None:
    entries = await discover(_python_tree(tmp_path), node=None)
    assert [(e.name, e.source, e.language) for e in entries] == [
        ("u", "user", "python"),
        ("h", "host", "python"),
    ]
    assert entries[0].meta == {"whenToUse": "use u"}


async def test_discover_python_workspace_allowed(tmp_path: Path) -> None:
    entries = await discover(
        _python_tree(tmp_path), node=None, python_sources=("workspace", "user", "host")
    )
    assert [e.name for e in entries] == ["w", "u", "h"]


def test_python_dirs_workspace_is_its_workflows_directory_only(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    trusted = python_dirs(dirs, ("workspace", "user", "host"))
    wd = tmp_path / "proj"
    assert wd / ".claude" / "workflows" in trusted
    assert wd not in trusted
    assert python_dirs(dirs, ("user", "host")) == (
        tmp_path / "user",
        *(tmp_path / f"host{i}" for i in (1, 2)),
    )


def test_python_dirs_needs_the_source_enabled(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path, sources=("user", "host"))
    trusted = python_dirs(dirs, ("workspace", "user", "host"))
    assert all("proj" not in str(path) for path in trusted)
    assert trusted


async def test_discover_python_js_wins_tie(tmp_path: Path) -> None:
    (tmp_path / "x.js").write_text("return 1\n", encoding="utf-8")
    (tmp_path / "x.py").write_text("meta = {}\n", encoding="utf-8")
    dirs = [SourceDir("user", tmp_path, USER_ID, "Workflows (user)")]
    entries = await discover(dirs, node=None)
    assert [(e.name, e.language) for e in entries] == [("x", "js")]


async def test_discover_python_bad_meta(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("meta = {'a': f()}\n", encoding="utf-8")
    dirs = [SourceDir("user", tmp_path, USER_ID, "Workflows (user)")]
    entries = await discover(dirs, node=None)
    assert [(e.name, e.meta, e.language) for e in entries] == [("bad", None, "python")]


async def test_customizations_python_marker(tmp_path: Path) -> None:
    (tmp_path / "p.py").write_text(
        "meta = {'phases': [{'title': 'Find'}], 'whenToUse': 'when', 'description': 'd'}\n",
        encoding="utf-8",
    )
    dirs = [SourceDir("user", tmp_path, USER_ID, "Workflows (user)")]
    containers = customizations(await discover(dirs, node=None), dirs, version="1")
    child = containers[0]["children"][0]
    assert child["_meta"][FLOW_META_KEY] == {
        "kind": "workflow",
        "phases": [{"title": "Find"}],
        "source": "user",
        "language": "python",
    }
    assert child["description"] == "when"


class _FakeReader:
    """An in-memory plugin: uri -> bytes; the listing is the keys directly under a uri."""

    def __init__(self, files: dict[str, bytes], *, fail: bool = False) -> None:
        self.files = files
        self.fail = fail
        self.reads: list[str] = []

    async def list_plugin_resource(self, uri: str) -> list[dict[str, str]]:
        if self.fail:
            raise RuntimeError("client gone")
        prefix = uri.rstrip("/") + "/"
        return [
            {"name": key[len(prefix) :], "type": "file"}
            for key in sorted(self.files)
            if key.startswith(prefix) and "/" not in key[len(prefix) :]
        ]

    async def read_plugin_resource(self, uri: str) -> bytes:
        self.reads.append(uri)
        return self.files[uri]


_PLUGIN = ClientPlugin("house", "plugin://client/house/", "House")
_WF = "plugin://client/house/workflows"


def _client_files() -> dict[str, bytes]:
    return {
        f"{_WF}/nightly.js": b"return 1\n",
        f"{_WF}/other.js": b"return 2\n",
        "plugin://client/house/skills/x.md": b"x",
    }


def test_client_plugins_parse_published_entries() -> None:
    plugins = client_plugins(
        [
            {"id": "a", "uri": "plugin://c/a", "name": "A", "nonce": "n", "clientId": "c1"},
            {"id": "a", "uri": "plugin://c/dup", "name": "Dup"},
            {"id": "b", "uri": 3, "name": "B"},
            {"uri": "plugin://c/x", "name": "X"},
        ]
    )
    assert plugins == [ClientPlugin("a", "plugin://c/a", "A", "n", "c1")]
    assert plugins[0].container_id == f"{CLIENT_PREFIX}a"
    assert plugins[0].workflows_uri == "plugin://c/a/workflows"


@pytest.mark.node
async def test_client_workflows_read_and_listed_first(node_path: str) -> None:
    reader = _FakeReader(_client_files())
    reader.files[f"{_WF}/nightly.js"] = b"export const meta = { whenToUse: 'nightly' }\n"
    entries = await read_client_workflows(_PLUGIN, reader, node=node_path)
    assert [(e.name, e.source, e.container_id) for e in entries] == [
        ("nightly", "client", f"{CLIENT_PREFIX}house"),
        ("other", "client", f"{CLIENT_PREFIX}house"),
    ]
    assert entries[0].meta is not None
    assert entries[0].location() == f"{_WF}/nightly.js"
    assert entries[0].source_text().startswith("export const meta")
    assert reader.reads == [f"{_WF}/nightly.js", f"{_WF}/other.js"]


async def test_client_workflows_without_node_have_no_meta() -> None:
    entries = await read_client_workflows(_PLUGIN, _FakeReader(_client_files()), node=None)
    assert [e.name for e in entries] == ["nightly", "other"]
    assert all(e.meta is None for e in entries)


async def test_client_py_never_read() -> None:
    files = _client_files()
    files[f"{_WF}/evil.py"] = b"meta = {}\n"
    reader = _FakeReader(files)
    entries = await read_client_workflows(_PLUGIN, reader, node=None)
    assert [e.name for e in entries] == ["nightly", "other"]
    assert f"{_WF}/evil.py" not in reader.reads


async def test_client_shadows_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "nightly.js").write_text("return 3\n", encoding="utf-8")
    dirs = [SourceDir("workspace", ws, "ahp-host-flow.workspace.0", "Workflows (ws)")]
    client = await read_client_workflows(_PLUGIN, _FakeReader(_client_files()), node=None)
    entries = await discover(dirs, node=None, client=client)
    assert [(e.source, e.name, e.shadowed_by) for e in entries] == [
        ("client", "nightly", None),
        ("client", "other", None),
        ("workspace", "nightly", "client"),
    ]
    assert _source(resolve(entries, "nightly")) == "client"
    workspace = resolve(entries, "workspace:nightly")
    assert workspace is not None
    assert workspace.path == ws / "nightly.js"
    assert workspace.location() == (ws / "nightly.js").as_uri()
    assert workspace.filename() == str(ws / "nightly.js")


async def test_client_container() -> None:
    client = await read_client_workflows(_PLUGIN, _FakeReader(_client_files()), node=None)
    empty = ClientPlugin("empty", "plugin://client/empty", "Empty")
    entries = await discover([], node=None, client=client)
    containers = customizations(entries, [], version="1", plugins=[empty, _PLUGIN])
    assert [c["id"] for c in containers] == [f"{CLIENT_PREFIX}house"]
    container = containers[0]
    assert container["type"] == "directory"
    assert container["uri"] == _WF
    assert container["name"] == "Workflows (House)"
    assert [c["id"] for c in container["children"]] == [
        f"{CLIENT_PREFIX}house.nightly",
        f"{CLIENT_PREFIX}house.other",
    ]
    assert container["children"][0]["uri"] == f"{_WF}/nightly.js"
    assert container["children"][0]["_meta"][FLOW_META_KEY]["source"] == "client"


async def test_client_bounds() -> None:
    files = _client_files()
    files[f"{_WF}/big.js"] = b"x" * (MAX_CLIENT_WORKFLOW_BYTES + 1)
    entries = await read_client_workflows(_PLUGIN, _FakeReader(files), node=None)
    assert [e.name for e in entries] == ["nightly", "other"]


async def test_client_workflow_count_bound() -> None:
    files = {f"{_WF}/w{i:04}.js": b"return 1\n" for i in range(MAX_CLIENT_WORKFLOWS + 5)}
    entries = await read_client_workflows(_PLUGIN, _FakeReader(files), node=None)
    assert len(entries) == MAX_CLIENT_WORKFLOWS


async def test_client_listing_failure() -> None:
    assert await read_client_workflows(_PLUGIN, _FakeReader({}, fail=True), node=None) == []


class _ListingReader(_FakeReader):
    """A client that lists names of its own choosing, whatever its files are."""

    def __init__(self, files: dict[str, bytes], names: list[str]) -> None:
        super().__init__(files)
        self.names = names

    async def list_plugin_resource(self, uri: str) -> list[dict[str, str]]:
        return [{"name": name, "type": "file"} for name in self.names]


async def test_client_listed_names_must_be_plain_file_names() -> None:
    files = _client_files()
    files["plugin://client/house/x.js"] = b"return 'escaped'\n"
    files[f"{_WF}/sub/y.js"] = b"return 'nested'\n"
    names = ["../x.js", "sub/y.js", "..\\x.js", "..", "nightly.js"]
    reader = _ListingReader(files, names)
    entries = await read_client_workflows(_PLUGIN, reader, node=None)
    assert [e.name for e in entries] == ["nightly"]
    assert reader.reads == [f"{_WF}/nightly.js"]


def test_flowext_schedule_of() -> None:
    assert schedule_of({"x": {"schedule": "0 6 * * 1-5"}}) == (
        {"expression": "0 6 * * 1-5", "timeZone": "UTC"},
        None,
    )
    berlin = {"expression": "30 7 * * *", "timeZone": "Europe/Berlin"}
    assert schedule_of({"x": {"schedule": berlin}}) == (berlin, None)

    schedule, warning = schedule_of({"x": {"schedule": "61 * * * *"}})
    assert schedule is None
    assert warning is not None
    assert warning.startswith("meta.x.schedule '61 * * * *' is not a valid schedule")

    nowhere = {"expression": "0 6 * * *", "timeZone": "Nowhere/Land"}
    schedule, warning = schedule_of({"x": {"schedule": nowhere}})
    assert schedule is None
    assert warning is not None
    assert warning.startswith("meta.x.schedule '0 6 * * *' is not a valid schedule")

    assert schedule_of({"x": {"schedule": 5}}) == (
        None,
        "meta.x.schedule must be a cron string or {expression, timeZone?}",
    )
    assert schedule_of({}) == (None, None)
    assert schedule_of(None) == (None, None)
    assert schedule_of({"x": "y"}) == (None, None)
