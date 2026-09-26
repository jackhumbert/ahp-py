"""The automation catalogue across machines.

Each node hosts its own automations; the surface sees one catalogue. What a
surface relies on: the capability appears when any machine can keep it, a new
automation lands on the machine its folder names, and everything after -
edits, runs, cancellation, the run's own channel - reaches the machine that
owns it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ahp_client import AhpClient
from ahp_client.client import ActionEvent, Subscription
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.core import InMemoryAutomationStore
from ahp_host.core.resources import RootedFilesystemResourceProvider
from ahp_host.provider.echo import EchoProvider
from ahp_protocol.channels import AUTOMATIONS_URI

from ahp_gateway.registry import NodeRecord
from tests.fleet import DEV, Fleet, everyone_is_a_dev


def _machine(base: Path, name: str, *, automations: bool = True) -> Host:
    root = base / name / "Github"
    (root / "project").mkdir(parents=True)
    return Host(
        EchoProvider(provider_id="claude"),
        LoopbackSingleUserPolicy(),
        resources=RootedFilesystemResourceProvider(root),
        default_directory=root.resolve().as_uri(),
        automations=InMemoryAutomationStore() if automations else None,
    )


def _fleet(tmp_path: Path, *names: str, without: tuple[str, ...] = ()) -> Fleet:
    return Fleet(
        {name: _machine(tmp_path, name, automations=name not in without) for name in names},
        [NodeRecord(name, f"mem://{name}", DEV) for name in names],
        everyone_is_a_dev,
    )


async def _connect(fleet: Fleet) -> tuple[AhpClient, dict[str, Any]]:
    raw = AhpClient(fleet.surface_transport())
    await raw.connect()
    return raw, await raw.initialize(client_id="phone")


def _definition(**session: Any) -> dict[str, Any]:
    return {
        "title": "Nightly",
        "message": {"text": "check the build", "origin": {"kind": "automation"}},
        "session": {"provider": "claude", **session},
        "enabled": True,
        "triggers": [],
    }


class _Watch:
    """Every action a subscription delivers, collected in the background."""

    def __init__(self, subscription: Subscription) -> None:
        self.envelopes: list[dict[str, Any]] = []
        self._task = asyncio.create_task(self._gather(subscription))

    async def _gather(self, subscription: Subscription) -> None:
        async for event in subscription:
            if isinstance(event, ActionEvent):
                self.envelopes.append(dict(event.envelope))

    def of(self, action_type: str) -> list[dict[str, Any]]:
        return [e for e in self.envelopes if e.get("action", {}).get("type") == action_type]

    async def until(self, ready: Callable[[], bool]) -> None:
        async with asyncio.timeout(5):
            while not ready():
                await asyncio.sleep(0.01)

    def close(self) -> None:
        self._task.cancel()


def _owned_by(fleet: Fleet, node: str) -> set[str]:
    return set(fleet.hosts[node]._automations)


async def test_one_catalogue_and_a_new_automation_goes_where_its_folder_is(
    tmp_path: Path,
) -> None:
    fleet = _fleet(tmp_path, "mac", "box")
    try:
        raw, handshake = await _connect(fleet)
        assert "create" in handshake["automations"]
        result, subscription = await raw.subscribe(AUTOMATIONS_URI)
        assert result["snapshot"]["state"] == {"entries": []}
        watch = _Watch(subscription)
        raw.dispatch(
            AUTOMATIONS_URI,
            {
                "type": "automation/createRequested",
                "resource": "ahp-automation:/on-box",
                "definition": _definition(workingDirectories=["ahp-file:///box/project"]),
            },
        )
        await watch.until(lambda: bool(watch.of("automation/set")))
        assert _owned_by(fleet, "box") == {"ahp-automation:/on-box"}
        assert _owned_by(fleet, "mac") == set()
        (entry,) = [e["action"]["automation"] for e in watch.of("automation/set")]
        # The node's own `file:` URI, qualified back into the gateway's tree.
        assert entry["definition"]["session"]["workingDirectories"] == ["ahp-file:///box/project"]
        watch.close()
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_runs_edits_and_removal_reach_the_owner(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, "mac", "box")
    try:
        raw, _ = await _connect(fleet)
        _, subscription = await raw.subscribe(AUTOMATIONS_URI)
        watch = _Watch(subscription)
        resource = "ahp-automation:/on-box"
        raw.dispatch(
            AUTOMATIONS_URI,
            {
                "type": "automation/createRequested",
                "resource": resource,
                "definition": _definition(workingDirectories=["ahp-file:///box/project"]),
            },
        )
        await watch.until(lambda: bool(watch.of("automation/set")))

        run = (
            await raw.request(
                "runAutomation",
                {"channel": AUTOMATIONS_URI, "automation": resource, "requestId": "r1"},
            )
        )["resource"]
        box = fleet.hosts["box"]
        await watch.until(lambda: box.sequencer.state_of(run)["lifecycle"]["status"] == "completed")
        snapshot, _ = await raw.subscribe(run)
        state = snapshot["snapshot"]["state"]
        assert state["lifecycle"]["status"] == "completed"
        listed = (await raw.request("listSessions", {}))["items"]
        (session,) = [item for item in listed if item.get("origin")]
        assert session["origin"]["run"] == run
        assert session["workingDirectories"] == ["ahp-file:///box/project"]

        raw.dispatch(
            AUTOMATIONS_URI,
            {
                "type": "automation/updateRequested",
                "resource": resource,
                "changes": {"title": "Renamed"},
            },
        )
        await watch.until(lambda: box._automations[resource].definition["title"] == "Renamed")
        raw.dispatch(AUTOMATIONS_URI, {"type": "automation/removed", "resource": resource})
        await watch.until(lambda: not _owned_by(fleet, "box"))
        watch.close()
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_no_folder_goes_to_the_first_machine_that_hosts_automations(
    tmp_path: Path,
) -> None:
    fleet = _fleet(tmp_path, "aaa", "mac", without=("aaa",))
    try:
        raw, handshake = await _connect(fleet)
        assert "automations" in handshake
        _, subscription = await raw.subscribe(AUTOMATIONS_URI)
        watch = _Watch(subscription)
        raw.dispatch(
            AUTOMATIONS_URI,
            {
                "type": "automation/createRequested",
                "resource": "ahp-automation:/chat",
                "definition": _definition(),
            },
        )
        await watch.until(lambda: bool(watch.of("automation/set")))
        assert _owned_by(fleet, "mac") == {"ahp-automation:/chat"}
        watch.close()
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_a_folder_on_a_machine_without_automations_is_refused(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, "mac", "box", without=("box",))
    try:
        raw, _ = await _connect(fleet)
        _, subscription = await raw.subscribe(AUTOMATIONS_URI)
        watch = _Watch(subscription)
        sent = raw.dispatch(
            AUTOMATIONS_URI,
            {
                "type": "automation/createRequested",
                "resource": "ahp-automation:/x",
                "definition": _definition(workingDirectories=["ahp-file:///box/project"]),
            },
        )
        await watch.until(lambda: bool(watch.of("automation/createRequested")))
        (echo,) = watch.of("automation/createRequested")
        assert echo["origin"]["clientSeq"] == sent.client_seq
        assert "automations" in echo["rejectionReason"]
        assert _owned_by(fleet, "mac") == set()
        watch.close()
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_without_any_host_there_is_no_catalogue(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, "mac", without=("mac",))
    try:
        raw, handshake = await _connect(fleet)
        assert "automations" not in handshake
        result, _ = await raw.subscribe(AUTOMATIONS_URI)
        assert "snapshot" not in result
        await raw.shutdown()
    finally:
        await fleet.aclose()


async def test_the_catalogue_is_every_machines_entries(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, "mac", "box")
    try:
        first, _ = await _connect(fleet)
        _, subscription = await first.subscribe(AUTOMATIONS_URI)
        watch = _Watch(subscription)
        for node in ("mac", "box"):
            first.dispatch(
                AUTOMATIONS_URI,
                {
                    "type": "automation/createRequested",
                    "resource": f"ahp-automation:/{node}",
                    "definition": _definition(workingDirectories=[f"ahp-file:///{node}/project"]),
                },
            )
        await watch.until(lambda: len(watch.of("automation/set")) == 2)
        watch.close()
        await first.shutdown()

        second, handshake = await _connect(fleet)
        assert handshake["automations"]["runHistoryLimit"] == 20
        result, _ = await second.subscribe(AUTOMATIONS_URI)
        resources = {e["resource"] for e in result["snapshot"]["state"]["entries"]}
        assert resources == {"ahp-automation:/mac", "ahp-automation:/box"}
        await second.shutdown()
    finally:
        await fleet.aclose()
