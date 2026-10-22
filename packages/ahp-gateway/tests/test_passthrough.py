"""Passthrough: one node's `file:` URIs cross the gateway verbatim, per connection.

Node `node-a` serves a real directory; `node-b` has no folders. A principal
whose passthrough node is `node-a` sees real absolute paths; everyone else
keeps the nested `ahp-file` layout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_client import AhpClient
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.core.resources import RootedFilesystemResourceProvider
from ahp_host.provider.echo import EchoProvider

from ahp_gateway.registry import NodeRecord, Principal
from tests.fleet import DEV, Fleet, everyone_is_a_dev


def _fleet(root: Path, passthrough: Any) -> Fleet:
    (root / "proj").mkdir(parents=True)
    (root / "proj" / "a.txt").write_text("hello")
    node_a = Host(
        EchoProvider(provider_id="claude"),
        LoopbackSingleUserPolicy(),
        resources=RootedFilesystemResourceProvider(root),
        default_directory=root.resolve().as_uri(),
    )
    node_b = Host(EchoProvider(provider_id="other"), LoopbackSingleUserPolicy())
    return Fleet(
        {"node-a": node_a, "node-b": node_b},
        [NodeRecord("node-a", "mem://a", DEV), NodeRecord("node-b", "mem://b", DEV)],
        everyone_is_a_dev,
        passthrough_node=passthrough,
    )


async def _connect(fleet: Fleet, client_id: str = "person-a") -> tuple[AhpClient, dict[str, Any]]:
    raw = AhpClient(fleet.surface_transport())
    await raw.connect()
    return raw, dict(await raw.initialize(client_id=client_id))


def _only_a(_principal: Principal) -> str | None:
    return "node-a"


async def test_the_default_directory_is_the_nodes_own(tmp_path: Path) -> None:
    root = tmp_path / "work"
    fleet = _fleet(root, "node-a")
    try:
        raw, init = await _connect(fleet)
        assert init["defaultDirectory"] == root.resolve().as_uri()
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_listing_and_reading_use_real_file_uris(tmp_path: Path) -> None:
    root = tmp_path / "work"
    fleet = _fleet(root, _only_a)
    try:
        raw, _ = await _connect(fleet)
        base = root.resolve().as_uri()
        listed = await raw.request("resourceList", {"uri": base})
        assert [e["name"] for e in listed["entries"]] == ["proj"]
        got = await raw.request("resourceRead", {"uri": f"{base}/proj/a.txt"})
        assert "hello" in str(got) or "aGVsbG8" in str(got)
        # The old spelling still routes.
        old = await raw.request("resourceList", {"uri": "ahp-file:///node-a/proj"})
        assert [e["name"] for e in old["entries"]] == ["a.txt"]
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_a_session_folder_lands_on_the_node_and_reads_back_real(tmp_path: Path) -> None:
    root = tmp_path / "work"
    fleet = _fleet(root, "node-a")
    try:
        raw, _ = await _connect(fleet)
        folder = f"{root.resolve().as_uri()}/proj"
        await raw.request(
            "createSession",
            {"channel": "claude:/s", "provider": "claude", "workingDirectories": [folder]},
        )
        listed = await raw.request("listSessions", {})
        assert [i["workingDirectories"] for i in listed["items"]] == [[folder]]
        assert {n for n, _ in fleet.connector.dialed} == {"node-a", "node-b"}
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_other_nodes_still_route_by_owner(tmp_path: Path) -> None:
    root = tmp_path / "work"
    fleet = _fleet(root, "node-a")
    try:
        raw, _ = await _connect(fleet)
        await raw.request("createSession", {"channel": "other:/s", "provider": "other"})
        listed = await raw.request("listSessions", {})
        assert [i["resource"] for i in listed["items"]] == ["other:/s"]
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_a_callable_returning_none_keeps_the_nested_layout(tmp_path: Path) -> None:
    root = tmp_path / "work"
    fleet = _fleet(root, lambda principal: "node-a" if principal.subject == "person-a" else None)
    try:
        passthrough, init_a = await _connect(fleet, "person-a")
        nested, init_b = await _connect(fleet, "person-b")
        assert init_a["defaultDirectory"] == root.resolve().as_uri()
        assert init_b["defaultDirectory"] == "ahp-file:///"
        top = await nested.request("resourceList", {"uri": "ahp-file:///"})
        assert [e["name"] for e in top["entries"]] == ["node-a", "node-b"]
        await passthrough.shutdown()
        await nested.shutdown()
    finally:
        await fleet.aclose()


@pytest.mark.parametrize("config", ["node-z", None])
async def test_an_unadmitted_or_absent_node_behaves_as_none(tmp_path: Path, config: Any) -> None:
    root = tmp_path / "work"
    fleet = _fleet(root, config)
    try:
        raw, init = await _connect(fleet)
        assert init["defaultDirectory"] == "ahp-file:///"
        await raw.shutdown()
    finally:
        await fleet.aclose()
