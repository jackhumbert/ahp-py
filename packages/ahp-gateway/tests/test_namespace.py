"""The `ahp-file` tree: a folder per node, each node's folder its own root.

Real hosts serving real directories (`RootedFilesystemResourceProvider` with a
`defaultDirectory`), reached through the gateway by a raw AHP client, so the
resource commands a folder picker sends are exercised end to end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ahp_client import AhpClient, RpcError
from ahp_client.client import ActionEvent, Subscription
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.core.resources import RootedFilesystemResourceProvider
from ahp_host.provider.echo import EchoProvider

from ahp_gateway.core.root import merge_root
from ahp_gateway.registry import NodeRecord
from tests.fleet import DEV, Fleet, everyone_is_a_dev


def _rooted_host(
    root: Path, provider: str = "claude", capabilities: dict[str, Any] | None = None
) -> Host:
    return Host(
        EchoProvider(provider_id=provider, capabilities=capabilities),
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
    mac = _tree(tmp_path, "mac", ["gateway/README.md", "notes.txt"])
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
        assert await _names(raw, "ahp-file:///mac") == ["gateway", "notes.txt"]
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
        assert await _names(raw, "file:///mac") == ["gateway", "notes.txt"]
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
    from ahp_gateway.core.uris import from_client_alias

    nodes = {"mac", "box"}
    assert from_client_alias("file:///mac/x", nodes) == "ahp-file:///mac/x"
    assert from_client_alias("file:///Users/me/x", nodes) == "file:///Users/me/x"
    assert from_client_alias("file:///", nodes) == "file:///"
    assert from_client_alias("file:///", nodes, root_uri=True) == "ahp-file:///"
    assert from_client_alias({"a": ["file:///box"]}, nodes) == {"a": ["ahp-file:///box"]}


async def _echoes(subscription: Subscription, count: int) -> list[dict[str, Any]]:
    """The next `count` working-directory echoes on a channel, then a beat more
    to catch any echo that should not have come."""
    found: list[dict[str, Any]] = []

    async def gather() -> None:
        async for event in subscription:
            if isinstance(event, ActionEvent) and str(
                event.envelope.get("action", {}).get("type")
            ).startswith("session/workingDirector"):
                found.append(event.envelope)

    task = asyncio.create_task(gather())
    try:
        async with asyncio.timeout(5):
            while len(found) < count:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
    return found


async def test_a_folder_on_another_machine_is_refused_out_loud(tmp_path: Path) -> None:
    # A session's agent loop lives on one node and cannot reach another's
    # disk. The gateway refuses, and echoes the refusal so the surface reverts
    # its optimistic prediction instead of showing a folder the agent lacks.
    multiroot = {"multipleWorkingDirectories": {"immutablePrimary": False}}
    mac = _tree(tmp_path, "mac", ["gateway/README.md"])
    box = _tree(tmp_path, "box", ["game/main.py", "tools/x.py"])
    fleet = Fleet(
        {
            "mac": _rooted_host(mac, capabilities=multiroot),
            "box": _rooted_host(box, capabilities=multiroot),
        },
        [NodeRecord("mac", "mem://mac", DEV), NodeRecord("box", "mem://box", DEV)],
        everyone_is_a_dev,
    )
    try:
        raw, _ = await _connect(fleet)
        channel = "claude:/on-box"
        await raw.request(
            "createSession",
            {
                "channel": channel,
                "provider": "claude",
                "workingDirectories": ["ahp-file:///box/game"],
            },
        )
        _, subscription = await raw.subscribe(channel)
        set_dir = "session/workingDirectorySet"
        foreign = raw.dispatch(channel, {"type": set_dir, "directory": "ahp-file:///mac/gateway"})
        local = raw.dispatch(channel, {"type": set_dir, "directory": "ahp-file:///box/tools"})
        refused, accepted = await _echoes(subscription, 2)

        assert refused["action"] == {"type": set_dir, "directory": "ahp-file:///mac/gateway"}
        assert refused["origin"] == {"clientId": "picker", "clientSeq": foreign.client_seq}
        assert "'mac'" in refused["rejectionReason"]
        assert "'box'" in refused["rejectionReason"]
        # The same-node one reached box, came back from it, and is shown in
        # the tree again.
        assert "rejectionReason" not in accepted
        assert accepted["origin"] == {"clientId": "picker", "clientSeq": local.client_seq}
        assert accepted["action"]["directory"] == "ahp-file:///box/tools"
        assert accepted["serverSeq"] > refused["serverSeq"]
        await raw.shutdown()

        async with fleet.direct("box") as direct_box:
            on_box = {i["resource"]: i for i in (await direct_box.sessions())["items"]}
        async with fleet.direct("mac") as direct_mac:
            on_mac = (await direct_mac.sessions())["items"]
        directories = on_box[channel]["workingDirectories"]
        assert [d.rsplit("/", 1)[-1] for d in directories] == ["game", "tools"]
        assert all("/box/Github/" in d for d in directories)
        assert not on_mac
    finally:
        await fleet.aclose()
