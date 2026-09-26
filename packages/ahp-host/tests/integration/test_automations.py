"""Host-owned automations, over the wire.

The properties a client relies on: the capability appears only when the host
can keep its promises; a definition is validated before it is saved, and a
rejection comes back as one; a run is a real session, with the saved message
as its first turn and `origin` pointing back at the run; schedules fire, and
missed ones follow `misfirePolicy`; and a restart neither loses an automation
nor leaves a run claiming to be in progress.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agent_host_protocol.channels import AUTOMATIONS_URI, ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.automations import (
    FileAutomationStore,
    InMemoryAutomationStore,
    iso,
)
from agent_host_server.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

AUTOMATION = "ahp-automation:/nightly"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _host(store: Any = None, **extra: Any) -> Host:
    return Host(
        EchoProvider(**extra.pop("echo", {})),
        LoopbackSingleUserPolicy(),
        automations=store,
        **extra,
    )


async def _client(host: Host, client_id: str = "c1") -> tuple[FakeClient, dict[str, Any]]:
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
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client, reply["result"]


def _definition(**overrides: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "title": "Nightly check",
        "message": {"text": "check the build", "origin": {"kind": "automation"}},
        "session": {"provider": "echo"},
        "enabled": True,
        "triggers": [],
    }
    definition.update(overrides)
    return definition


def _schedule(expression: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": "t1",
        "kind": "schedule",
        "schedule": {"expression": expression, "timeZone": "UTC"},
        **extra,
    }


async def _dispatch(client: FakeClient, channel: str, action: dict[str, Any], seq: int = 1) -> None:
    await client.notify("dispatchAction", {"channel": channel, "clientSeq": seq, "action": action})


def _echo_of(client: FakeClient, action_type: str) -> dict[str, Any] | None:
    for message in client.notifications:
        envelope = message.get("params", {})
        action = envelope.get("action", {})
        if message.get("method") == "action" and action.get("type") == action_type:
            return dict(envelope)
    return None


def _entries(host: Host) -> list[dict[str, Any]]:
    return list(host.sequencer.state_of(AUTOMATIONS_URI)["entries"])


def _status(host: Host, run: str) -> str:
    return str(host.sequencer.state_of(run)["lifecycle"]["status"])


async def _until(check: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def _create(client: FakeClient, host: Host, definition: dict[str, Any]) -> None:
    await _dispatch(
        client,
        AUTOMATIONS_URI,
        {"type": "automation/createRequested", "resource": AUTOMATION, "definition": definition},
    )
    await _until(lambda: any(e["resource"] == AUTOMATION for e in _entries(host)))


class TestOffWithoutAStore:
    async def test_no_capability_no_channel_and_a_named_refusal(self) -> None:
        host = _host()
        try:
            client, result = await _client(host)
            assert "automations" not in result
            assert not host.sequencer.has_channel(AUTOMATIONS_URI)
            reply = await client.request(
                "runAutomation",
                {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r"},
            )
            assert reply["error"]["code"] == -32009
        finally:
            await host.aclose()


class TestTheCatalogue:
    async def test_advertised_and_subscribable(self) -> None:
        host = _host(InMemoryAutomationStore())
        try:
            client, result = await _client(host)
            assert result["automations"]["runHistoryLimit"] == 20
            assert "create" in result["automations"]
            snapshot = (await client.request("subscribe", {"channel": AUTOMATIONS_URI}))["result"]
            assert snapshot["snapshot"]["state"] == {"entries": []}
            listed = await client.request("listAutomationTriggerDefinitions", {"channel": ROOT_URI})
            assert listed["result"] == {"items": []}
        finally:
            await host.aclose()

    async def test_create_publishes_and_persists(self, tmp_path: Path) -> None:
        host = _host(FileAutomationStore(tmp_path))
        try:
            client, _ = await _client(host)
            await _create(client, host, _definition(triggers=[_schedule("0 9 * * *")]))
            (entry,) = _entries(host)
            assert entry["definition"]["title"] == "Nightly check"
            assert entry["operations"] == ["update", "remove", "run"]
            assert entry["runs"] == []
            assert "nextRunAt" in entry
            (saved,) = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
            assert saved["resource"] == AUTOMATION
        finally:
            await host.aclose()

    @pytest.mark.parametrize(
        ("definition", "reason"),
        [
            (_definition(message={"text": "x", "origin": {"kind": "user"}}), "origin.kind"),
            (_definition(triggers=[_schedule("61 * * * *")]), "outside"),
            (_definition(triggers=[_schedule("0 9 * * *", misfirePolicy="later")]), "misfire"),
            (
                _definition(
                    triggers=[
                        {"id": "e", "kind": "event", "type": "push", "title": "P", "events": []}
                    ]
                ),
                "no event trigger",
            ),
            (_definition(triggers=[_schedule("* * * * *"), _schedule("0 * * * *")]), "share"),
            (_definition(session={"provider": "nope"}), "no agent"),
        ],
    )
    async def test_an_invalid_definition_is_rejected(
        self, definition: dict[str, Any], reason: str
    ) -> None:
        host = _host(InMemoryAutomationStore())
        try:
            client, _ = await _client(host)
            await client.request("subscribe", {"channel": AUTOMATIONS_URI})
            await _dispatch(
                client,
                AUTOMATIONS_URI,
                {
                    "type": "automation/createRequested",
                    "resource": AUTOMATION,
                    "definition": definition,
                },
            )
            await client.collect_until(
                lambda: _echo_of(client, "automation/createRequested") is not None
            )
            echo = _echo_of(client, "automation/createRequested")
            assert echo is not None
            assert reason in echo["rejectionReason"]
            assert _entries(host) == []
        finally:
            await host.aclose()

    async def test_requests_belong_on_the_catalogue(self) -> None:
        host = _host(InMemoryAutomationStore())
        try:
            client, _ = await _client(host)
            await _dispatch(
                client,
                ROOT_URI,
                {
                    "type": "automation/createRequested",
                    "resource": AUTOMATION,
                    "definition": _definition(),
                },
            )
            await client.collect_until(
                lambda: _echo_of(client, "automation/createRequested") is not None
            )
            assert "belongs on" in _echo_of(client, "automation/createRequested")["rejectionReason"]  # type: ignore[index]
            assert _entries(host) == []
        finally:
            await host.aclose()

    async def test_update_patches_and_remove_forgets(self, tmp_path: Path) -> None:
        host = _host(FileAutomationStore(tmp_path))
        try:
            client, _ = await _client(host)
            await _create(client, host, _definition())
            await _dispatch(
                client,
                AUTOMATIONS_URI,
                {
                    "type": "automation/updateRequested",
                    "resource": AUTOMATION,
                    "changes": {"title": "Renamed", "enabled": False},
                },
                seq=2,
            )
            await _until(lambda: _entries(host)[0]["definition"]["title"] == "Renamed")
            definition = _entries(host)[0]["definition"]
            assert definition["enabled"] is False
            assert definition["message"]["text"] == "check the build"
            await _dispatch(
                client,
                AUTOMATIONS_URI,
                {"type": "automation/removed", "resource": AUTOMATION},
                seq=3,
            )
            await _until(lambda: not list(tmp_path.glob("*.json")))
            assert _entries(host) == []
        finally:
            await host.aclose()


class TestARun:
    async def test_a_manual_run_is_a_session_with_the_saved_message(self) -> None:
        host = _host(InMemoryAutomationStore())
        try:
            client, _ = await _client(host)
            await _create(client, host, _definition())
            reply = await client.request(
                "runAutomation",
                {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r1"},
            )
            run = reply["result"]["resource"]
            assert run.startswith("ahp-automation-run:")
            await _until(lambda: _status(host, run) == "completed")
            state = host.sequencer.state_of(run)
            assert state["origin"] == {"kind": "manual"}
            assert state["lifecycle"]["startedAt"] <= state["lifecycle"]["completedAt"]
            (session,) = state["sessions"]
            assert state["primarySession"] == session
            session_state = host.sequencer.state_of(session)
            assert session_state["origin"] == {
                "kind": "automation",
                "automation": AUTOMATION,
                "run": run,
            }
            assert session_state["title"] == "Nightly check"
            chat = host.sequencer.state_of(session_state["defaultChat"])
            (turn,) = chat["turns"]
            assert turn["message"]["text"] == "check the build"
            assert turn["message"]["origin"] == {"kind": "automation"}
            (summary,) = _entries(host)[0]["runs"]
            assert summary["resource"] == run
            assert summary["sessionCount"] == 1
            assert summary["lifecycle"]["status"] == "completed"
            listed = await client.request("listSessions", {"channel": ROOT_URI})
            (item,) = listed["result"]["items"]
            assert item["origin"]["run"] == run
        finally:
            await host.aclose()

    async def test_the_request_id_makes_a_retry_idempotent(self) -> None:
        host = _host(InMemoryAutomationStore())
        try:
            client, _ = await _client(host)
            await _create(client, host, _definition())
            params = {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "same"}
            first = (await client.request("runAutomation", params))["result"]["resource"]
            second = (await client.request("runAutomation", params))["result"]["resource"]
            assert first == second
            await _until(lambda: _status(host, first) == "completed")
            assert len(_entries(host)[0]["runs"]) == 1
        finally:
            await host.aclose()

    async def test_an_unknown_automation_is_not_found(self) -> None:
        host = _host(InMemoryAutomationStore())
        try:
            client, _ = await _client(host)
            reply = await client.request(
                "runAutomation",
                {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r"},
            )
            assert reply["error"]["code"] == -32008
        finally:
            await host.aclose()

    async def test_a_run_can_be_cancelled(self) -> None:
        host = _host(InMemoryAutomationStore(), echo={"delay": 5.0})
        try:
            client, _ = await _client(host)
            await _create(client, host, _definition())
            run = (
                await client.request(
                    "runAutomation",
                    {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r"},
                )
            )["result"]["resource"]
            await _until(lambda: _status(host, run) == "running")
            await client.request("subscribe", {"channel": run})
            await _dispatch(client, run, {"type": "automationRun/cancelRequested"}, seq=2)
            await _until(lambda: _status(host, run) == "cancelled")
            assert "startedAt" in host.sequencer.state_of(run)["lifecycle"]
        finally:
            await host.aclose()

    async def test_a_missing_provider_fails_the_run_not_the_host(self) -> None:
        store = InMemoryAutomationStore()
        host = _host(store)
        try:
            client, _ = await _client(host)
            await _create(client, host, _definition())
            # A provider that existed when the automation was saved and does
            # not now -- "The host revalidates every selection when the run
            # starts."
            host._automations[AUTOMATION].definition["session"]["provider"] = "gone"
            run = (
                await client.request(
                    "runAutomation",
                    {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r"},
                )
            )["result"]["resource"]
            await _until(lambda: _status(host, run) == "failed")
            assert "gone" in host.sequencer.state_of(run)["lifecycle"]["error"]["message"]
        finally:
            await host.aclose()

    async def test_history_pages_in(self) -> None:
        host = _host(InMemoryAutomationStore(), automation_history=1)
        try:
            client, _ = await _client(host)
            await _create(client, host, _definition())
            for n in range(3):
                run = (
                    await client.request(
                        "runAutomation",
                        {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": f"{n}"},
                    )
                )["result"]["resource"]
                await _until(lambda run=run: _status(host, run) == "completed")  # type: ignore[misc]
            entry = _entries(host)[0]
            assert len(entry["runs"]) == 1
            assert entry["runsNextCursor"] == "1"
            await client.request(
                "fetchAutomationRuns",
                {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "cursor": "1"},
            )
            entry = _entries(host)[0]
            assert len(entry["runs"]) == 2
            assert entry["runsNextCursor"] == "2"
        finally:
            await host.aclose()


class TestSchedules:
    async def test_a_due_schedule_starts_a_triggered_run(self) -> None:
        host = _host(InMemoryAutomationStore())
        try:
            client, _ = await _client(host)
            await _create(client, host, _definition(triggers=[_schedule("*/5 * * * *")]))
            now = datetime.now(UTC) + timedelta(minutes=10)
            assert await host.run_due_automations(now) == 1
            (summary,) = _entries(host)[0]["runs"]
            origin = summary["origin"]
            assert origin["kind"] == "trigger"
            assert origin["triggerId"] == "t1"
            assert "catchUp" not in origin
            await _until(lambda: _status(host, summary["resource"]) == "completed")
            # Dealt with: the same instant does not fire twice.
            assert await host.run_due_automations(now) == 0
        finally:
            await host.aclose()

    async def test_missed_occurrences_collapse_into_one_catch_up(self) -> None:
        host = _host(InMemoryAutomationStore())
        try:
            client, _ = await _client(host)
            await _create(client, host, _definition(triggers=[_schedule("0 * * * *")]))
            now = datetime.now(UTC) + timedelta(days=2, minutes=30)
            assert await host.run_due_automations(now) == 1
            (summary,) = _entries(host)[0]["runs"]
            assert summary["origin"]["catchUp"] is True
        finally:
            await host.aclose()

    async def test_skip_drops_missed_occurrences(self) -> None:
        host = _host(InMemoryAutomationStore())
        try:
            client, _ = await _client(host)
            await _create(
                client, host, _definition(triggers=[_schedule("0 * * * *", misfirePolicy="skip")])
            )
            now = datetime.now(UTC) + timedelta(days=2, minutes=30)
            assert await host.run_due_automations(now) == 0
            assert _entries(host)[0]["nextRunAt"] > iso(now)
        finally:
            await host.aclose()

    async def test_a_disabled_automation_does_not_fire(self) -> None:
        host = _host(InMemoryAutomationStore())
        try:
            client, _ = await _client(host)
            await _create(
                client, host, _definition(enabled=False, triggers=[_schedule("* * * * *")])
            )
            assert await host.run_due_automations(datetime.now(UTC) + timedelta(hours=1)) == 0
            assert "nextRunAt" not in _entries(host)[0]
        finally:
            await host.aclose()


class TestRestart:
    async def test_automations_come_back_and_an_interrupted_run_is_failed(
        self, tmp_path: Path
    ) -> None:
        first = _host(FileAutomationStore(tmp_path), echo={"delay": 5.0})
        client, _ = await _client(first)
        await _create(client, first, _definition())
        run = (
            await client.request(
                "runAutomation",
                {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r"},
            )
        )["result"]["resource"]
        await _until(lambda: _status(first, run) == "running")
        await first.aclose()

        second = _host(FileAutomationStore(tmp_path))
        try:
            await second.restore()
            (entry,) = _entries(second)
            assert entry["definition"]["title"] == "Nightly check"
            assert _status(second, run) == "failed"
            assert entry["runs"][0]["lifecycle"]["status"] == "failed"
            client, _ = await _client(second)
            snapshot = (await client.request("subscribe", {"channel": run}))["result"]
            assert snapshot["snapshot"]["state"]["resource"] == run
        finally:
            await second.aclose()
