"""The `ahp-file` tree: a folder per node, each node's folder its own root.

Real hosts serving real directories (`RootedFilesystemResourceProvider` with a
`defaultDirectory`), reached through the broker by a raw AHP client, so the
resource commands a folder picker sends are exercised end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_host_client import AhpClient, RpcError
from agent_host_server import Host, LoopbackSingleUserPolicy
from agent_host_server.core.resources import RootedFilesystemResourceProvider
from agent_host_server.provider.echo import EchoProvider

from agent_host_broker.core.root import merge_root
from agent_host_broker.registry import NodeRecord
from tests.fleet import DEV, Fleet, everyone_is_a_dev


def _rooted_host(root: Path, provider: str = "claude") -> Host:
    return Host(
        EchoProvider(provider_id=provider),
        LoopbackSingleUserPolicy(),
        resources=RootedFilesystemResourceProvider(root),
        default_directory=root.resolve().as_uri(),
    )


def _tree(base: Path, name: str, files: list[str]) -> Path:
    root = base / name / "Github"
    for file in files:
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        (root / file).write_text(file)
    return root


@pytest.fixture
def two_machines(tmp_path: Path) -> Fleet:
    mac = _tree(tmp_path, "mac", ["broker/README.md", "notes.txt"])
    box = _tree(tmp_path, "box", ["game/main.py"])
    return Fleet(
        {"mac": _rooted_host(mac), "box": _rooted_host(box)},
        [NodeRecord("mac", "mem://mac", DEV), NodeRecord("box", "mem://box", DEV)],
        everyone_is_a_dev,
    )


async def _connect(fleet: Fleet) -> tuple[AhpClient, dict[str, object]]:
    raw = AhpClient(fleet.surface_transport())
    await raw.connect()
    return raw, await raw.initialize(client_id="picker")


async def _names(raw: AhpClient, uri: str) -> list[str]:
    listed = await raw.request("resourceList", {"uri": uri})
    return sorted(entry["name"] for entry in listed["entries"])


async def test_the_picker_starts_at_the_list_of_machines(two_machines: Fleet) -> None:
    try:
        raw, result = await _connect(two_machines)
        assert result["defaultDirectory"] == "ahp-file:///"
        # Inventory order (by node id), which is also the default machine for
        # plain chats.
        listed = await raw.request("resourceList", {"uri": "ahp-file:///"})
        assert listed["entries"] == [
            {"name": "box", "type": "directory"},
            {"name": "mac", "type": "directory"},
        ]
        resolved = await raw.request("resourceResolve", {"uri": "ahp-file:///"})
        assert resolved["type"] == "directory"
        await raw.shutdown()
    finally:
        await two_machines.aclose()


async def test_each_machines_folder_is_its_root(two_machines: Fleet) -> None:
    try:
        raw, _ = await _connect(two_machines)
        assert await _names(raw, "ahp-file:///mac") == ["broker", "notes.txt"]
        assert await _names(raw, "ahp-file:///box") == ["game"]
        assert await _names(raw, "ahp-file:///box/game") == ["main.py"]
        read = await raw.request("resourceRead", {"uri": "ahp-file:///mac/notes.txt"})
        assert "notes.txt" in str(read)
        await raw.shutdown()
    finally:
        await two_machines.aclose()


async def test_the_list_of_machines_is_read_only(two_machines: Fleet) -> None:
    try:
        raw, _ = await _connect(two_machines)
        with pytest.raises(RpcError) as caught:
            await raw.request("resourceMkdir", {"uri": "ahp-file:///"})
        assert caught.value.code == -32009
        await raw.shutdown()
    finally:
        await two_machines.aclose()


async def test_climbing_out_of_a_machines_root_is_refused(two_machines: Fleet) -> None:
    try:
        raw, _ = await _connect(two_machines)
        with pytest.raises(RpcError) as caught:
            await raw.request("resourceList", {"uri": "ahp-file:///mac/../../box"})
        assert caught.value.code == -32602
        await raw.shutdown()
    finally:
        await two_machines.aclose()


async def test_a_folder_decides_the_machine_and_a_plain_chat_takes_the_first_by_id(
    two_machines: Fleet,
) -> None:
    try:
        raw, _ = await _connect(two_machines)
        on_box = await raw.request(
            "createSession",
            {
                "channel": "claude:/on-box",
                "provider": "claude",
                "workingDirectories": ["ahp-file:///box/game"],
            },
        )
        assert on_box is None or isinstance(on_box, dict)
        plain = await raw.request(
            "createSession", {"channel": "claude:/plain", "provider": "claude"}
        )
        assert plain is None or isinstance(plain, dict)
        listed = await raw.request("listSessions", {})
        by_uri = {item["resource"]: item for item in listed["items"]}
        # The surface sees the folder in the tree; the node saw its own path.
        assert by_uri["claude:/on-box"]["workingDirectories"] == ["ahp-file:///box/game"]
        await raw.shutdown()
        async with two_machines.direct("box") as box:
            on_box_node = {i["resource"]: i for i in (await box.sessions())["items"]}
        async with two_machines.direct("mac") as mac:
            on_mac_node = {i["resource"] for i in (await mac.sessions())["items"]}
        assert on_box_node["claude:/on-box"]["workingDirectories"][0].endswith("/box/Github/game")
        # No folder: the first node by id ("box") takes the plain chat.
        assert "claude:/plain" in on_box_node
        assert not on_mac_node
    finally:
        await two_machines.aclose()


async def test_session_settings_before_a_folder_come_from_the_first_machine(
    two_machines: Fleet,
) -> None:
    try:
        raw, _ = await _connect(two_machines)
        # Ambiguous by provider alone; answered rather than refused.
        await raw.request("resolveSessionConfig", {"provider": "claude"})
        await raw.shutdown()
    finally:
        await two_machines.aclose()


async def test_one_machine_opens_the_picker_at_its_root(tmp_path: Path) -> None:
    mac = _tree(tmp_path, "mac", ["a.txt"])
    fleet = Fleet(
        {"mac": _rooted_host(mac)}, [NodeRecord("mac", "mem://mac", DEV)], everyone_is_a_dev
    )
    try:
        raw, result = await _connect(fleet)
        assert result["defaultDirectory"] == "ahp-file:///mac"
        await raw.shutdown()
    finally:
        await fleet.aclose()


def test_one_agent_on_two_machines_offers_both_machines_models() -> None:
    merged = merge_root(
        [
            {"agents": [{"provider": "claude", "models": [{"id": "default"}, {"id": "opus"}]}]},
            {"agents": [{"provider": "claude", "models": [{"id": "opus"}, {"id": "fable"}]}]},
        ]
    )
    (agent,) = merged["agents"]
    assert [model["id"] for model in agent["models"]] == ["default", "opus", "fable"]


async def test_vscode_browses_the_tree_as_file_paths(two_machines: Fleet) -> None:
    # VS Code keeps only the path of `defaultDirectory` and browses as `file:`.
    try:
        raw, _ = await _connect(two_machines)
        top = await raw.request("resourceList", {"uri": "file:///"})
        assert [e["name"] for e in top["entries"]] == ["box", "mac"]
        assert await _names(raw, "file:///mac") == ["broker", "notes.txt"]
        assert await _names(raw, "file:///box/game") == ["main.py"]
        await raw.request(
            "createSession",
            {
                "channel": "claude:/picked",
                "provider": "claude",
                "workingDirectories": ["file:///box/game"],
            },
        )
        await raw.shutdown()
        async with two_machines.direct("box") as box:
            on_box = {i["resource"]: i for i in (await box.sessions())["items"]}
        assert on_box["claude:/picked"]["workingDirectories"][0].endswith("/box/Github/game")
    finally:
        await two_machines.aclose()


def test_only_a_node_named_first_segment_is_an_alias() -> None:
    from agent_host_broker.core.uris import from_client_alias

    nodes = {"mac", "box"}
    assert from_client_alias("file:///mac/x", nodes) == "ahp-file:///mac/x"
    assert from_client_alias("file:///Users/me/x", nodes) == "file:///Users/me/x"
    assert from_client_alias("file:///", nodes) == "file:///"
    assert from_client_alias("file:///", nodes, root_uri=True) == "ahp-file:///"
    assert from_client_alias({"a": ["file:///box"]}, nodes) == {"a": ["ahp-file:///box"]}
