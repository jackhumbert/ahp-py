"""Flat places: `ahp-file:///<node>: <root>/...`, one path segment per (node, root).

An editor labels a folder by the last segment of its URI, so the node has to
live in that segment. Nodes here serve named roots from a small in-memory tree.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from ahp_client import AhpClient
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.core.resources import (
    DirectoryEntry,
    ResourceContent,
    ResourceInfo,
)
from ahp_host.provider.echo import EchoProvider
from ahp_protocol.errors import AhpError

from ahp_gateway.core.uris import (
    from_client_alias,
    node_of,
    qualify_file_uris,
    unqualify_file_uris,
)
from ahp_gateway.registry import NodeRecord
from tests.fleet import DEV, Fleet, everyone_is_a_dev
from tests.links import RecordingLink, connection


class _Roots:
    """`file:///<root>/<dir>/<file>` over a dict of roots; the top lists the roots."""

    def __init__(self, tree: dict[str, dict[str, str]]) -> None:
        self.tree = tree

    def _parts(self, uri: str) -> list[str]:
        return [part for part in uri.removeprefix("file:///").split("/") if part]

    async def resolve(self, uri: str, *, follow_symlinks: bool = True) -> ResourceInfo:
        parts = self._parts(uri)
        if not parts or (len(parts) == 1 and parts[0] in self.tree):
            return ResourceInfo(uri, "directory")
        if len(parts) == 2 and parts[1] in self.tree.get(parts[0], {}):
            return ResourceInfo(uri, "file")
        raise AhpError(-32008, f"No such resource: {uri}")

    async def read(self, uri: str) -> ResourceContent:
        parts = self._parts(uri)
        if len(parts) == 2 and parts[1] in self.tree.get(parts[0], {}):
            return ResourceContent(self.tree[parts[0]][parts[1]].encode())
        raise AhpError(-32008, f"No such resource: {uri}")

    async def list_dir(self, uri: str) -> Sequence[DirectoryEntry]:
        parts = self._parts(uri)
        if not parts:
            return [DirectoryEntry(name, "directory") for name in self.tree]
        if len(parts) == 1 and parts[0] in self.tree:
            return [DirectoryEntry(name, "file") for name in self.tree[parts[0]]]
        raise AhpError(-32008, f"No such resource: {uri}")


class _NoListing:
    """A node whose top is not a listing of roots."""

    async def resolve(self, uri: str, *, follow_symlinks: bool = True) -> ResourceInfo:
        raise AhpError(-32008, f"No such resource: {uri}")

    async def read(self, uri: str) -> ResourceContent:
        raise AhpError(-32008, f"No such resource: {uri}")

    async def list_dir(self, uri: str) -> Sequence[DirectoryEntry]:
        raise AhpError(-32008, f"No such resource: {uri}")


def _host(resources: Any) -> Host:
    return Host(
        EchoProvider(provider_id="claude"),
        LoopbackSingleUserPolicy(),
        resources=resources,
    )


def _fleet(**options: Any) -> Fleet:
    node_a = _host(_Roots({"proj": {"a.txt": "hello"}, "docs": {}}))
    node_b = _host(_NoListing())
    return Fleet(
        {"node-a": node_a, "node-b": node_b},
        [NodeRecord("node-a", "mem://a", DEV), NodeRecord("node-b", "mem://b", DEV)],
        everyone_is_a_dev,
        **options,
    )


async def _connect(fleet: Fleet, client_id: str = "picker") -> AhpClient:
    raw = AhpClient(fleet.surface_transport())
    await raw.connect()
    await raw.initialize(client_id=client_id)
    return raw


async def test_the_top_lists_one_entry_per_root_and_a_bare_id_without_roots() -> None:
    fleet = _fleet(flat_places=True)
    try:
        raw = await _connect(fleet)
        listed = await raw.request("resourceList", {"uri": "ahp-file:///"})
        assert sorted(e["name"] for e in listed["entries"]) == [
            "node-a: docs",
            "node-a: proj",
            "node-b",
        ]
        assert {e["type"] for e in listed["entries"]} == {"directory"}
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_a_callable_decides_per_connection_from_the_principal() -> None:
    fleet = _fleet(flat_places=lambda principal: principal.subject == "person-a")
    try:
        flat = await _connect(fleet, "person-a")
        nested = await _connect(fleet, "person-b")
        flat_top = await flat.request("resourceList", {"uri": "ahp-file:///"})
        nested_top = await nested.request("resourceList", {"uri": "ahp-file:///"})
        assert sorted(e["name"] for e in flat_top["entries"]) == [
            "node-a: docs",
            "node-a: proj",
            "node-b",
        ]
        assert sorted(e["name"] for e in nested_top["entries"]) == ["node-a", "node-b"]
        flat_dir = await flat.request("resourceList", {"uri": "ahp-file:///node-a: proj"})
        assert [e["name"] for e in flat_dir["entries"]] == ["a.txt"]
        nested_dir = await nested.request("resourceList", {"uri": "ahp-file:///node-a/proj"})
        assert [e["name"] for e in nested_dir["entries"]] == ["a.txt"]
        await flat.shutdown()
        await nested.shutdown()
    finally:
        await fleet.aclose()


async def test_the_top_is_unchanged_by_default() -> None:
    fleet = _fleet()
    try:
        raw = await _connect(fleet)
        listed = await raw.request("resourceList", {"uri": "ahp-file:///"})
        assert [e["name"] for e in listed["entries"]] == ["node-a", "node-b"]
        await raw.shutdown()
    finally:
        await fleet.aclose()


@pytest.mark.parametrize(
    "folder",
    [
        "ahp-file:///node-a: proj/sub",
        "ahp-file:///node-a:%20proj/sub",
        "ahp-file:/node-a:%20proj/sub",
        "file:///node-a: proj/sub",
    ],
)
async def test_a_working_directory_in_a_flat_place_reaches_its_node(folder: str) -> None:
    fleet = _fleet(flat_places=True)
    try:
        raw = await _connect(fleet)
        await raw.request(
            "createSession",
            {"channel": "claude:/s", "provider": "claude", "workingDirectories": [folder]},
        )
        listed = await raw.request("listSessions", {})
        # Back out, the surface sees the flat form.
        assert [i["workingDirectories"] for i in listed["items"]] == [
            ["ahp-file:///node-a:%20proj/sub"]
        ]
        await raw.shutdown()
        async with fleet.direct("node-a") as node:
            items = (await node.sessions())["items"]
        assert [i["workingDirectories"] for i in items] == [["file:///proj/sub"]]
    finally:
        await fleet.aclose()


async def test_the_nested_form_still_routes() -> None:
    fleet = _fleet(flat_places=True)
    try:
        raw = await _connect(fleet)
        await raw.request(
            "createSession",
            {
                "channel": "claude:/nested",
                "provider": "claude",
                "workingDirectories": ["ahp-file:///node-a/proj"],
            },
        )
        await raw.shutdown()
        async with fleet.direct("node-a") as node:
            items = (await node.sessions())["items"]
        assert [i["workingDirectories"] for i in items] == [["file:///proj"]]
    finally:
        await fleet.aclose()


async def test_resources_list_and_read_through_a_flat_place() -> None:
    fleet = _fleet(flat_places=True)
    try:
        raw = await _connect(fleet)
        listed = await raw.request("resourceList", {"uri": "ahp-file:///node-a: proj"})
        assert listed["entries"] == [{"name": "a.txt", "type": "file"}]
        again = await raw.request("resourceList", {"uri": "ahp-file:/node-a:%20proj"})
        assert again["entries"] == listed["entries"]
        read = await raw.request("resourceRead", {"uri": "ahp-file:///node-a:%20proj/a.txt"})
        assert "aGVsbG8=" in str(read) or "hello" in str(read)
        resolved = await raw.request("resourceResolve", {"uri": "ahp-file:///node-a: proj/a.txt"})
        assert resolved["uri"] == "ahp-file:///node-a:%20proj/a.txt"
        await raw.shutdown()
    finally:
        await fleet.aclose()


def test_uri_mapping_both_ways() -> None:
    out = qualify_file_uris("file:///proj/sub dir/x", "node-a", flat=True)
    assert out == "ahp-file:///node-a:%20proj/sub%20dir/x"
    assert qualify_file_uris("file:///", "node-a", flat=True) == "ahp-file:///node-a"
    assert qualify_file_uris("file:///proj", "node-a", flat=True) == "ahp-file:///node-a:%20proj"
    assert unqualify_file_uris(out, "node-a", flat=True) == "file:///proj/sub%20dir/x"
    assert unqualify_file_uris("ahp-file:///node-a/proj", "node-a", flat=True) == "file:///proj"
    assert node_of("ahp-file:///node-a:%20proj/x", flat=True) == "node-a"
    assert from_client_alias("file:///node-a: proj/x", {"node-a"}, flat=True) == (
        "ahp-file:///node-a: proj/x"
    )
    # Off: the colon form is not a place.
    assert from_client_alias("file:///node-a: proj/x", {"node-a"}) == "file:///node-a: proj/x"


@pytest.mark.parametrize("root", [None, "srv/work"])
def test_a_node_without_named_roots_has_one_spelling_both_ways(root: str | None) -> None:
    """Listed as its bare id, so its files are the nested `ahp-file:///<node>/...`, out too."""
    conn = connection(RecordingLink("n"))
    conn.flat_places = True
    conn.nodes["n"].path_root = root
    local = f"file:///{root}/a.py" if root else "file:///a.py"
    assert conn._unqualify("ahp-file:///n/a.py", "n") == local
    assert conn._qualify(local, "n", root) == "ahp-file:///n/a.py"
    # A node that does serve named roots keeps the flat spelling.
    conn.nodes["n"].places = True
    assert conn._qualify("file:///proj/a.py", "n", root) == "ahp-file:///n:%20proj/a.py"


class _Files:
    """A node whose top holds files, not named roots: `file:///<name>`."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def _name(self, uri: str) -> str:
        return uri.removeprefix("file:///").strip("/")

    async def resolve(self, uri: str, *, follow_symlinks: bool = True) -> ResourceInfo:
        name = self._name(uri)
        if not name:
            return ResourceInfo(uri, "directory")
        if name in self.files:
            return ResourceInfo(uri, "file")
        raise AhpError(-32008, f"No such resource: {uri}")

    async def read(self, uri: str) -> ResourceContent:
        if self._name(uri) in self.files:
            return ResourceContent(self.files[self._name(uri)].encode())
        raise AhpError(-32008, f"No such resource: {uri}")

    async def list_dir(self, uri: str) -> Sequence[DirectoryEntry]:
        if not self._name(uri):
            return [DirectoryEntry(name, "file") for name in self.files]
        raise AhpError(-32008, f"No such resource: {uri}")


async def test_a_node_without_named_roots_resolves_back_to_the_spelling_asked() -> None:
    fleet = Fleet(
        {"node-a": _host(_Roots({"proj": {}})), "node-c": _host(_Files({"a.txt": "hello"}))},
        [NodeRecord("node-a", "mem://a", DEV), NodeRecord("node-c", "mem://c", DEV)],
        everyone_is_a_dev,
        flat_places=True,
    )
    try:
        raw = await _connect(fleet)
        top = await raw.request("resourceList", {"uri": "ahp-file:///"})
        assert sorted(e["name"] for e in top["entries"]) == ["node-a: proj", "node-c"]
        listed = await raw.request("resourceList", {"uri": "ahp-file:///node-c"})
        assert [e["name"] for e in listed["entries"]] == ["a.txt"]
        resolved = await raw.request("resourceResolve", {"uri": "ahp-file:///node-c/a.txt"})
        assert resolved["uri"] == "ahp-file:///node-c/a.txt"
        await raw.shutdown()
    finally:
        await fleet.aclose()


def test_a_place_on_another_node_is_refused() -> None:
    from ahp_gateway.core.uris import ForeignUriError

    with pytest.raises(ForeignUriError):
        unqualify_file_uris("ahp-file:///node-b: proj/x", "node-a", flat=True)
    with pytest.raises(ForeignUriError):
        unqualify_file_uris("ahp-file:///node-a: proj/../../x", "node-a", flat=True)


def test_a_node_id_containing_the_separator_is_refused() -> None:
    # A node id is lowercase DNS labels, so no record can carry the separator.
    with pytest.raises(ValueError, match="node-a: x"):
        NodeRecord("node-a: x", "mem://x", DEV)
