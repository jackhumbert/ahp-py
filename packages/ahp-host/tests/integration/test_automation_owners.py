"""Automations belong to the principal that created them.

From the other user's side, as `test_multi_user.py` does for sessions: bob
cannot see, update, remove, run, page the runs of, or cancel a run of alice's
automation, and alice can. The owner is host-private: it is persisted on the
record and never appears in an entry.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import AUTOMATIONS_URI, ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import ConnectionInfo, Host, LoopbackSingleUserPolicy
from ahp_host.core.automations import (
    AutomationRecord,
    FileAutomationStore,
    InMemoryAutomationStore,
)
from ahp_host.core.policies import OwnedSessionPolicy, principal_from_header
from ahp_host.provider import EchoProvider

from .test_automations import (
    AUTOMATION,
    _definition,
    _dispatch,
    _echo_of,
    _entries,
    _until,
)
from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_HEADER = "x-forwarded-user"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def shared() -> AsyncIterator[tuple[Host, OwnedSessionPolicy]]:
    policy = OwnedSessionPolicy(principal_from_header(_HEADER))
    host = Host(EchoProvider(), policy, automations=InMemoryAutomationStore())
    try:
        yield host, policy
    finally:
        await host.aclose()


async def _user(host: Host, user: str, client_id: str) -> tuple[FakeClient, dict[str, Any]]:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport, peer="proxy", headers={_HEADER: user}))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    reply = await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["0.9.0"],
            "initialSubscriptions": [ROOT_URI, AUTOMATIONS_URI],
        },
    )
    return client, reply["result"]


def _catalogue(result: dict[str, Any]) -> list[str]:
    (snapshot,) = [s for s in result["snapshots"] if s["resource"] == AUTOMATIONS_URI]
    return [e["resource"] for e in snapshot["state"]["entries"]]


async def _alice_creates(host: Host) -> tuple[FakeClient, FakeClient]:
    alice, _ = await _user(host, "alice", "a1")
    bob, _ = await _user(host, "bob", "b1")
    await _dispatch(
        alice,
        AUTOMATIONS_URI,
        {
            "type": "automation/createRequested",
            "resource": AUTOMATION,
            "definition": _definition(),
        },
    )
    await _until(lambda: any(e["resource"] == AUTOMATION for e in _entries(host)))
    await alice.collect(seconds=0.1)
    await bob.collect(seconds=0.1)
    return alice, bob


class TestVisibility:
    async def test_the_owner_sees_it_and_the_other_user_does_not(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        await _alice_creates(host)
        _, alice_result = await _user(host, "alice", "a2")
        _, bob_result = await _user(host, "bob", "b2")
        assert _catalogue(alice_result) == [AUTOMATION]
        assert _catalogue(bob_result) == []

    async def test_a_live_subscriber_is_not_sent_anyone_elses_automation(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        alice, bob = await _alice_creates(host)
        assert _echo_of(alice, "automation/set") is not None
        for message in bob.notifications:
            assert "nightly" not in json.dumps(message)

    async def test_a_create_request_is_not_echoed_to_another_user_live_or_on_replay(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        alice, bob = await _alice_creates(host)
        assert _echo_of(alice, "automation/createRequested") is not None
        for message in bob.notifications:
            assert "createRequested" not in json.dumps(message)
            assert "nightly" not in json.dumps(message)
        reply = await bob.request(
            "reconnect",
            {"clientId": "b1", "lastSeenServerSeq": 0, "subscriptions": [AUTOMATIONS_URI]},
        )
        await bob.collect(seconds=0.1)
        assert "createRequested" not in json.dumps(reply["result"])
        for message in bob.notifications:
            assert "createRequested" not in json.dumps(message)

    async def test_a_borrowed_client_id_is_not_sent_anyone_elses_definitions(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        # A clientId is client-asserted: bob can claim alice's. The origin
        # echo is hers by identity, not by the id she happened to pick.
        host, _ = shared
        alice, _ = await _user(host, "alice", "a1")
        bob, _ = await _user(host, "bob", "a1")
        await _dispatch(
            alice,
            AUTOMATIONS_URI,
            {
                "type": "automation/createRequested",
                "resource": AUTOMATION,
                "definition": _definition(),
            },
        )
        await _dispatch(
            alice,
            AUTOMATIONS_URI,
            {
                "type": "automation/createRequested",
                "resource": "ahp-automation:/rejected",
                "definition": {**_definition(), "title": 7, "message": {"text": "secret"}},
            },
            seq=2,
        )
        await _until(lambda: any(e["resource"] == AUTOMATION for e in _entries(host)))
        await alice.collect_until(
            lambda: _echo_of(alice, "automation/createRequested") is not None, timeout=2
        )
        await bob.collect(seconds=0.2)
        for message in bob.notifications:
            assert "nightly" not in json.dumps(message)
            assert "secret" not in json.dumps(message)
        reply = await bob.request(
            "reconnect",
            {"clientId": "a1", "lastSeenServerSeq": 0, "subscriptions": [AUTOMATIONS_URI]},
        )
        await bob.collect(seconds=0.1)
        assert "nightly" not in json.dumps(reply["result"])
        assert "secret" not in json.dumps(reply["result"])
        # Alice, reconnecting, still gets her own echoes, the refusal included.
        resumed = await alice.request(
            "reconnect",
            {"clientId": "a1", "lastSeenServerSeq": 0, "subscriptions": [AUTOMATIONS_URI]},
        )
        assert "secret" in json.dumps(resumed["result"])

    async def test_the_owner_is_never_on_the_wire(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        alice, _ = await _alice_creates(host)
        assert "alice" not in json.dumps(host.sequencer.state_of(AUTOMATIONS_URI))
        assert "alice" not in json.dumps(alice.notifications)

    async def test_a_reconnect_snapshot_is_filtered_too(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        _, bob = await _alice_creates(host)
        reply = await bob.request(
            "reconnect",
            {"clientId": "b1", "lastSeenServerSeq": 0, "subscriptions": [AUTOMATIONS_URI]},
        )
        result = reply["result"]
        assert "nightly" not in json.dumps(result)

    async def test_a_second_user_can_subscribe_to_the_catalogue(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        _, bob = await _alice_creates(host)
        reply = await bob.request("subscribe", {"channel": AUTOMATIONS_URI})
        assert reply["result"]["snapshot"]["state"] == {"entries": []}


class TestMutation:
    async def test_the_other_user_cannot_update_or_remove(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        alice, bob = await _alice_creates(host)
        await _dispatch(
            bob,
            AUTOMATIONS_URI,
            {
                "type": "automation/updateRequested",
                "resource": AUTOMATION,
                "changes": {"title": "mine now"},
            },
            seq=1,
        )
        await _dispatch(
            bob, AUTOMATIONS_URI, {"type": "automation/removed", "resource": AUTOMATION}, seq=2
        )
        await bob.collect_until(lambda: _echo_of(bob, "automation/removed") is not None, timeout=2)
        for kind in ("automation/updateRequested", "automation/removed"):
            echo = _echo_of(bob, kind)
            assert echo is not None
            assert echo["rejectionReason"] == "rejected by policy"
        (entry,) = _entries(host)
        assert entry["definition"]["title"] == "Nightly check"
        # Alice, who owns it, is told nothing of the attempt.
        await alice.collect(seconds=0.1)
        assert _echo_of(alice, "automation/removed") is None

    async def test_the_owner_can_update_then_remove(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        alice, _ = await _alice_creates(host)
        await _dispatch(
            alice,
            AUTOMATIONS_URI,
            {
                "type": "automation/updateRequested",
                "resource": AUTOMATION,
                "changes": {"title": "renamed"},
            },
            seq=1,
        )
        await _until(lambda: _entries(host)[0]["definition"]["title"] == "renamed")
        await _dispatch(
            alice, AUTOMATIONS_URI, {"type": "automation/removed", "resource": AUTOMATION}, seq=2
        )
        await _until(lambda: _entries(host) == [])

    async def test_the_other_user_cannot_run_or_page_runs(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        _, bob = await _alice_creates(host)
        run = await bob.request(
            "runAutomation",
            {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r1"},
        )
        assert run["error"]["code"] == -32009
        runs = await bob.request(
            "fetchAutomationRuns", {"channel": AUTOMATIONS_URI, "automation": AUTOMATION}
        )
        assert runs["error"]["code"] == -32009
        assert _entries(host)[0]["runs"] == []

    async def test_the_owner_can_run_observe_and_cancel_the_run(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        alice, bob = await _alice_creates(host)
        reply = await alice.request(
            "runAutomation",
            {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r1"},
        )
        run = reply["result"]["resource"]
        watched = await alice.request("subscribe", {"channel": run})
        assert "error" not in watched
        refused = await bob.request("subscribe", {"channel": run})
        assert refused["error"]["code"] == -32009
        # Not even by naming it on reconnect.
        resumed = await bob.request(
            "reconnect", {"clientId": "b1", "lastSeenServerSeq": 0, "subscriptions": [run]}
        )
        assert run not in json.dumps(resumed["result"].get("snapshots", []))


class TestRunCancelByOwnerOnly:
    async def test_cancel_is_refused_for_the_other_user_while_the_run_is_live(self) -> None:
        policy = OwnedSessionPolicy(principal_from_header(_HEADER))
        host = Host(EchoProvider(delay=5.0), policy, automations=InMemoryAutomationStore())
        try:
            alice, bob = await _alice_creates(host)
            reply = await alice.request(
                "runAutomation",
                {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r1"},
            )
            run = reply["result"]["resource"]
            await alice.request("subscribe", {"channel": run})
            await _dispatch(
                bob, run, {"type": "automationRun/cancelRequested", "resource": run}, seq=1
            )
            await bob.collect(seconds=0.2)
            # Bob is not subscribed to the run, so the refusal is not echoed to
            # him; what matters is that nothing happened.
            assert host.sequencer.state_of(run)["lifecycle"]["status"] != "cancelled"
            await _dispatch(
                alice, run, {"type": "automationRun/cancelRequested", "resource": run}, seq=1
            )
            await _until(lambda: host.sequencer.state_of(run)["lifecycle"]["status"] == "cancelled")
        finally:
            await host.aclose()


class TestScheduledRunsBelongToTheOwner:
    async def test_the_run_channel_and_its_session_are_the_owners(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, policy = shared
        await _alice_creates(host)
        (record,) = host._automations.values()
        run = await host._start_run(record, {"kind": "trigger", "triggerId": "t"})
        assert policy.owner_of(run) == "alice"
        await _until(lambda: "primarySession" in host.sequencer.state_of(run))
        session = host.sequencer.state_of(run)["primarySession"]
        assert policy.owner_of(session) == "alice"

    async def test_a_scheduled_run_session_is_alices_and_hidden_from_bob(self) -> None:
        policy = OwnedSessionPolicy(principal_from_header(_HEADER))
        host = Host(EchoProvider(), policy, automations=InMemoryAutomationStore())
        try:
            alice, _ = await _user(host, "alice", "a1")
            bob, _ = await _user(host, "bob", "b1")
            await _dispatch(
                alice,
                AUTOMATIONS_URI,
                {
                    "type": "automation/createRequested",
                    "resource": AUTOMATION,
                    "definition": _definition(
                        triggers=[
                            {
                                "id": "t1",
                                "kind": "schedule",
                                "schedule": {"expression": "*/5 * * * *", "timeZone": "UTC"},
                            }
                        ]
                    ),
                },
            )
            await _until(lambda: any(e["resource"] == AUTOMATION for e in _entries(host)))
            now = datetime.now(UTC) + timedelta(minutes=10)
            assert await host.run_due_automations(now) == 1
            (summary,) = _entries(host)[0]["runs"]
            run = summary["resource"]
            await _until(lambda: "primarySession" in host.sequencer.state_of(run))
            session = host.sequencer.state_of(run)["primarySession"]
            assert policy.owner_of(run) == "alice"
            assert policy.owner_of(session) == "alice"
            for channel in (run, session):
                assert "error" not in await alice.request("subscribe", {"channel": channel})
                refused = await bob.request("subscribe", {"channel": channel})
                assert refused["error"]["code"] == -32009
        finally:
            await host.aclose()

    async def test_a_manual_run_session_is_alices_and_hidden_from_bob(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, policy = shared
        alice, bob = await _alice_creates(host)
        reply = await alice.request(
            "runAutomation",
            {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r1"},
        )
        run = reply["result"]["resource"]
        await _until(lambda: "primarySession" in host.sequencer.state_of(run))
        session = host.sequencer.state_of(run)["primarySession"]
        assert policy.owner_of(run) == "alice"
        assert policy.owner_of(session) == "alice"
        for channel in (run, session):
            refused = await bob.request("subscribe", {"channel": channel})
            assert refused["error"]["code"] == -32009

    async def test_an_ownerless_automation_makes_an_ownerless_run_session(self) -> None:
        policy = OwnedSessionPolicy(principal_from_header(_HEADER))
        store = InMemoryAutomationStore()
        await store.save(
            AutomationRecord(
                resource=AUTOMATION,
                definition=_definition(),
                created_at="2026-01-01T00:00:00.000Z",
                modified_at="2026-01-01T00:00:00.000Z",
            )
        )
        host = Host(EchoProvider(), policy, automations=store)
        try:
            await _user(host, "alice", "a1")
            (record,) = host._automations.values()
            assert record.principal is None
            run = await host._start_run(record, {"kind": "trigger", "triggerId": "t"})
            await _until(lambda: "primarySession" in host.sequencer.state_of(run))
            session = host.sequencer.state_of(run)["primarySession"]
            assert policy.owner_of(run) is None
            assert policy.owner_of(session) is None
        finally:
            await host.aclose()


class TestPersistence:
    async def test_the_principal_survives_a_restart_and_still_scopes(self, tmp_path: Path) -> None:
        policy = OwnedSessionPolicy(principal_from_header(_HEADER))
        first = Host(EchoProvider(), policy, automations=FileAutomationStore(tmp_path))
        try:
            await _alice_creates(first)
        finally:
            await first.aclose()
        (saved,) = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
        assert saved["principal"] == "alice"

        second = Host(
            EchoProvider(),
            OwnedSessionPolicy(principal_from_header(_HEADER)),
            automations=FileAutomationStore(tmp_path),
        )
        try:
            _, alice = await _user(second, "alice", "a1")
            _, bob = await _user(second, "bob", "b1")
            assert _catalogue(alice) == [AUTOMATION]
            assert _catalogue(bob) == []
        finally:
            await second.aclose()

    def test_a_record_without_a_principal_loads_as_none(self) -> None:
        legacy = {
            "format": AutomationRecord(
                resource=AUTOMATION, definition={}, created_at="x", modified_at="x"
            ).to_json()["format"],
            "resource": AUTOMATION,
            "definition": _definition(),
            "createdAt": "2026-01-01T00:00:00.000Z",
            "modifiedAt": "2026-01-01T00:00:00.000Z",
        }
        record = AutomationRecord.from_json(legacy)
        assert record.principal is None
        assert "principal" not in record.to_json()

    async def test_a_legacy_record_is_everyones(self, tmp_path: Path) -> None:
        store = FileAutomationStore(tmp_path)
        await store.save(
            AutomationRecord(
                resource=AUTOMATION,
                definition=_definition(),
                created_at="2026-01-01T00:00:00.000Z",
                modified_at="2026-01-01T00:00:00.000Z",
            )
        )
        host = Host(
            EchoProvider(),
            OwnedSessionPolicy(principal_from_header(_HEADER)),
            automations=FileAutomationStore(tmp_path),
        )
        try:
            _, alice = await _user(host, "alice", "a1")
            _, bob = await _user(host, "bob", "b1")
            assert _catalogue(alice) == [AUTOMATION]
            assert _catalogue(bob) == [AUTOMATION]
        finally:
            await host.aclose()


class TestPolicyHook:
    async def test_may_manage_automation_decides_when_a_policy_has_one(self) -> None:
        asked: list[tuple[str, str | None]] = []

        class Admin(OwnedSessionPolicy):
            def may_manage_automation(
                self, info: ConnectionInfo, uri: str, owner: str | None
            ) -> bool:
                asked.append((uri, owner))
                return self.principal_of(info) == "root" or super().may_manage_automation(
                    info, uri, owner
                )

        host = Host(
            EchoProvider(),
            Admin(principal_from_header(_HEADER)),
            automations=InMemoryAutomationStore(),
        )
        try:
            await _alice_creates(host)
            _, root = await _user(host, "root", "r1")
            _, bob = await _user(host, "bob", "b2")
            assert _catalogue(root) == [AUTOMATION]
            assert _catalogue(bob) == []
            assert (AUTOMATION, "alice") in asked
        finally:
            await host.aclose()

    async def test_a_raising_hook_refuses(self) -> None:
        class Broken(OwnedSessionPolicy):
            def may_manage_automation(
                self, info: ConnectionInfo, uri: str, owner: str | None
            ) -> bool:
                raise RuntimeError("boom")

        host = Host(
            EchoProvider(),
            Broken(principal_from_header(_HEADER)),
            automations=InMemoryAutomationStore(),
        )
        try:
            alice, _ = await _alice_creates(host)
            reply = await alice.request(
                "runAutomation",
                {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r"},
            )
            assert reply["error"]["code"] == -32009
        finally:
            await host.aclose()


class TestSingleUserPolicyIsUnchanged:
    async def test_no_principal_means_everyone_sees_and_manages_all(self) -> None:
        host = Host(
            EchoProvider(), LoopbackSingleUserPolicy(), automations=InMemoryAutomationStore()
        )
        try:
            first, _ = await _user_loopback(host, "c1")
            second, result = await _user_loopback(host, "c2")
            await _dispatch(
                first,
                AUTOMATIONS_URI,
                {
                    "type": "automation/createRequested",
                    "resource": AUTOMATION,
                    "definition": _definition(),
                },
            )
            await _until(lambda: any(e["resource"] == AUTOMATION for e in _entries(host)))
            (record,) = host._automations.values()
            assert record.principal is None
            reply = await second.request(
                "runAutomation",
                {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r"},
            )
            assert "error" not in reply
            assert _catalogue(result) == []
            await second.request("subscribe", {"channel": AUTOMATIONS_URI})
            # The other client also receives the create echo (no principal).
            frames = await second.collect(seconds=0.2)
            assert any(AUTOMATION in json.dumps(f) for f in frames)
        finally:
            await host.aclose()


async def _user_loopback(host: Host, client_id: str) -> tuple[FakeClient, dict[str, Any]]:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    reply = await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["0.9.0"],
            "initialSubscriptions": [ROOT_URI, AUTOMATIONS_URI],
        },
    )
    return client, reply["result"]


class _HomeFolders(OwnedSessionPolicy):
    """Each principal may grant only folders under `/Users/<principal>`."""

    def __init__(self) -> None:
        super().__init__(principal_from_header(_HEADER), allow_resources=True)
        self.revoked: set[str] = set()

    def _theirs(self, principal: str | None, directory: str) -> bool:
        return (
            principal is not None
            and principal not in self.revoked
            and directory.startswith(f"file:///Users/{principal}/")
        )

    def may_grant_working_directory(
        self, info: ConnectionInfo, session: str, directory: str
    ) -> bool:
        return self._theirs(self.principal_of(info), directory)

    def may_grant_working_directory_for(
        self, principal: str | None, session: str, directory: str
    ) -> bool:
        return self._theirs(principal, directory)


def _with_folder(folder: str) -> dict[str, Any]:
    return _definition(session={"provider": "echo", "workingDirectories": [folder]})


def _saved(definition: dict[str, Any], principal: str | None) -> AutomationRecord:
    return AutomationRecord(
        resource=AUTOMATION,
        definition=definition,
        created_at="2026-01-01T00:00:00.000Z",
        modified_at="2026-01-01T00:00:00.000Z",
        principal=principal,
    )


async def _run_session_folders(host: Host, client: FakeClient) -> list[str]:
    reply = await client.request(
        "runAutomation",
        {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r1"},
    )
    run = reply["result"]["resource"]
    await _until(lambda: "primarySession" in host.sequencer.state_of(run))
    session = host.sequencer.state_of(run)["primarySession"]
    await _until(lambda: host.sequencer.state_of(session).get("lifecycle") == "ready")
    return list(host.sequencer.state_of(session).get("workingDirectories") or [])


class TestWorkingDirectories:
    async def test_the_default_policy_refuses_a_folder_at_create(
        self, shared: tuple[Host, OwnedSessionPolicy]
    ) -> None:
        host, _ = shared
        alice, _ = await _user(host, "alice", "a1")
        await _dispatch(
            alice,
            AUTOMATIONS_URI,
            {
                "type": "automation/createRequested",
                "resource": AUTOMATION,
                "definition": _with_folder("file:///Users/bob/secrets"),
            },
        )
        await alice.collect_until(
            lambda: _echo_of(alice, "automation/createRequested") is not None, timeout=2
        )
        echo = _echo_of(alice, "automation/createRequested")
        assert echo is not None
        assert "rejected by policy" in echo["rejectionReason"]
        assert _entries(host) == []

    async def test_another_users_folder_is_refused_at_create_and_update(self) -> None:
        host = Host(EchoProvider(), _HomeFolders(), automations=InMemoryAutomationStore())
        try:
            alice, _ = await _user(host, "alice", "a1")
            await _dispatch(
                alice,
                AUTOMATIONS_URI,
                {
                    "type": "automation/createRequested",
                    "resource": AUTOMATION,
                    "definition": _with_folder("file:///Users/bob/secrets"),
                },
            )
            await alice.collect_until(
                lambda: _echo_of(alice, "automation/createRequested") is not None, timeout=2
            )
            assert "rejectionReason" in (_echo_of(alice, "automation/createRequested") or {})
            assert _entries(host) == []
            await _dispatch(
                alice,
                AUTOMATIONS_URI,
                {
                    "type": "automation/createRequested",
                    "resource": AUTOMATION,
                    "definition": _with_folder("file:///Users/alice/proj"),
                },
                seq=2,
            )
            await _until(lambda: any(e["resource"] == AUTOMATION for e in _entries(host)))
            await _dispatch(
                alice,
                AUTOMATIONS_URI,
                {
                    "type": "automation/updateRequested",
                    "resource": AUTOMATION,
                    "changes": {
                        "session": {
                            "provider": "echo",
                            "workingDirectories": ["file:///Users/bob/secrets"],
                        }
                    },
                },
                seq=3,
            )
            await alice.collect_until(
                lambda: _echo_of(alice, "automation/updateRequested") is not None, timeout=2
            )
            assert "rejectionReason" in (_echo_of(alice, "automation/updateRequested") or {})
            (entry,) = _entries(host)
            assert entry["definition"]["session"]["workingDirectories"] == [
                "file:///Users/alice/proj"
            ]
            assert await _run_session_folders(host, alice) == ["file:///Users/alice/proj"]
        finally:
            await host.aclose()

    async def test_a_run_rechecks_the_folder_against_the_owner(self) -> None:
        policy = _HomeFolders()
        host = Host(EchoProvider(), policy, automations=InMemoryAutomationStore())
        try:
            alice, _ = await _user(host, "alice", "a1")
            await _dispatch(
                alice,
                AUTOMATIONS_URI,
                {
                    "type": "automation/createRequested",
                    "resource": AUTOMATION,
                    "definition": _with_folder("file:///Users/alice/proj"),
                },
            )
            await _until(lambda: any(e["resource"] == AUTOMATION for e in _entries(host)))
            policy.revoked.add("alice")
            assert await _run_session_folders(host, alice) == []
        finally:
            await host.aclose()

    async def test_a_saved_foreign_folder_is_not_granted_to_a_run(self) -> None:
        # Saved before the create-time check existed, naming bob's folder for
        # alice: the run asks again, for alice.
        store = InMemoryAutomationStore()
        await store.save(_saved(_with_folder("file:///Users/bob/secrets"), "alice"))
        host = Host(EchoProvider(), _HomeFolders(), automations=store)
        try:
            alice, _ = await _user(host, "alice", "a1")
            assert await _run_session_folders(host, alice) == []
        finally:
            await host.aclose()

    async def test_a_multi_user_policy_without_the_hook_grants_a_run_nothing(self) -> None:
        class NoHook(OwnedSessionPolicy):
            def __getattribute__(self, name: str) -> Any:
                if name == "may_grant_working_directory_for":
                    raise AttributeError(name)
                return super().__getattribute__(name)

            def may_grant_working_directory(
                self, info: ConnectionInfo, session: str, directory: str
            ) -> bool:
                return True

        store = InMemoryAutomationStore()
        await store.save(_saved(_with_folder("file:///Users/alice/proj"), "alice"))
        host = Host(EchoProvider(), NoHook(principal_from_header(_HEADER)), automations=store)
        try:
            alice, _ = await _user(host, "alice", "a1")
            assert await _run_session_folders(host, alice) == []
        finally:
            await host.aclose()

    async def test_a_single_user_run_keeps_its_folder(self) -> None:
        host = Host(
            EchoProvider(), LoopbackSingleUserPolicy(), automations=InMemoryAutomationStore()
        )
        try:
            client, _ = await _user_loopback(host, "c1")
            await _dispatch(
                client,
                AUTOMATIONS_URI,
                {
                    "type": "automation/createRequested",
                    "resource": AUTOMATION,
                    "definition": _with_folder("file:///Users/me/proj"),
                },
            )
            await _until(lambda: any(e["resource"] == AUTOMATION for e in _entries(host)))
            assert await _run_session_folders(host, client) == ["file:///Users/me/proj"]
        finally:
            await host.aclose()
