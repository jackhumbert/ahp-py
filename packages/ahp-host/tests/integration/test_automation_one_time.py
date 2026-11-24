"""One-time automation triggers (`kind: "oneTime"`, upstream 1.2.0, unreleased).

What a client relies on: the host says it supports them; a trigger fires at
most once for a given `runAt`, across updates and restarts; moving `runAt`
moves the occurrence; a `runAt` missed while the host was away follows
`misfirePolicy` like a schedule; and `nextRunAt` shows the pending time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ahp_host.core.automations import FileAutomationStore, InMemoryAutomationStore, iso

from .test_automations import (
    _client,
    _create,
    _definition,
    _entry,
    _host,
    _until,
    _update,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _one_time(run_at: datetime, **extra: Any) -> dict[str, Any]:
    return {"id": "once", "kind": "oneTime", "runAt": iso(run_at), **extra}


def _runs(host: Any) -> list[dict[str, Any]]:
    return list(_entry(host)["runs"])


async def test_the_capability_is_advertised() -> None:
    host = _host(InMemoryAutomationStore())
    try:
        _, result = await _client(host)
        assert result["automations"]["oneTime"] == {}
    finally:
        await host.aclose()


async def test_it_fires_once_at_run_at() -> None:
    host = _host(InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        run_at = datetime.now(UTC) + timedelta(hours=1)
        await _create(client, host, _definition(triggers=[_one_time(run_at)]))
        assert _entry(host)["nextRunAt"] == iso(run_at)
        assert await host.run_due_automations(run_at - timedelta(minutes=1)) == 0
        assert await host.run_due_automations(run_at + timedelta(seconds=30)) == 1
        (summary,) = _runs(host)
        assert summary["origin"] == {
            "kind": "trigger",
            "triggerId": "once",
            "scheduledFor": iso(run_at),
        }
        assert "nextRunAt" not in _entry(host)
        assert await host.run_due_automations(run_at + timedelta(minutes=2)) == 0
        assert await host.run_due_automations(run_at + timedelta(days=3)) == 0
    finally:
        await host.aclose()


async def test_a_missed_run_at_catches_up_once() -> None:
    host = _host(InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        run_at = datetime.now(UTC) + timedelta(hours=1)
        await _create(client, host, _definition(triggers=[_one_time(run_at)]))
        late = run_at + timedelta(hours=6)
        assert await host.run_due_automations(late) == 1
        (summary,) = _runs(host)
        assert summary["origin"]["catchUp"] is True
        assert summary["origin"]["scheduledFor"] == iso(run_at)
        assert await host.run_due_automations(late + timedelta(hours=1)) == 0
    finally:
        await host.aclose()


async def test_skip_drops_a_missed_run_at() -> None:
    host = _host(InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        run_at = datetime.now(UTC) + timedelta(hours=1)
        await _create(client, host, _definition(triggers=[_one_time(run_at, misfirePolicy="skip")]))
        assert await host.run_due_automations(run_at + timedelta(hours=6)) == 0
        assert _runs(host) == []
        assert "nextRunAt" not in _entry(host)
    finally:
        await host.aclose()


async def test_moving_run_at_moves_the_occurrence() -> None:
    host = _host(InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        first = datetime.now(UTC) + timedelta(minutes=30)
        await _create(client, host, _definition(triggers=[_one_time(first)]))
        assert await host.run_due_automations(first + timedelta(seconds=10)) == 1
        # Same `runAt` saved again, with another policy: still dealt with.
        await _update(client, host, {"triggers": [_one_time(first, misfirePolicy="skip")]}, seq=2)
        assert await host.run_due_automations(first + timedelta(minutes=1)) == 0
        # A new time is a new occurrence.
        second = datetime.now(UTC) + timedelta(hours=2)
        await _update(client, host, {"triggers": [_one_time(second)]}, seq=3)
        assert _entry(host)["nextRunAt"] == iso(second)
        assert await host.run_due_automations(second + timedelta(seconds=10)) == 1
        assert len(_runs(host)) == 2
    finally:
        await host.aclose()


async def test_moving_run_at_before_it_fires_drops_the_old_time() -> None:
    host = _host(InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        old = datetime.now(UTC) + timedelta(hours=1)
        new = datetime.now(UTC) + timedelta(hours=3)
        await _create(client, host, _definition(triggers=[_one_time(old)]))
        await _update(client, host, {"triggers": [_one_time(new)]}, seq=2)
        assert await host.run_due_automations(old + timedelta(seconds=10)) == 0
        assert await host.run_due_automations(new + timedelta(seconds=10)) == 1
    finally:
        await host.aclose()


async def test_a_run_at_long_past_when_saved_never_fires() -> None:
    host = _host(InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        await _create(
            client, host, _definition(triggers=[_one_time(datetime.now(UTC) - timedelta(days=1))])
        )
        assert await host.run_due_automations(datetime.now(UTC)) == 0
        assert "nextRunAt" not in _entry(host)
    finally:
        await host.aclose()


async def test_a_run_at_of_just_now_fires_on_time() -> None:
    host = _host(InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        run_at = datetime.now(UTC) - timedelta(seconds=20)
        await _create(client, host, _definition(triggers=[_one_time(run_at)]))
        # The scheduler may get there first; either way, exactly one run.
        await host.run_due_automations(datetime.now(UTC))
        await _until(lambda: len(_runs(host)) == 1)
        assert "catchUp" not in _runs(host)[0]["origin"]
        assert await host.run_due_automations(datetime.now(UTC)) == 0
    finally:
        await host.aclose()


async def test_a_disabled_automation_does_not_fire() -> None:
    host = _host(InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        run_at = datetime.now(UTC) + timedelta(hours=1)
        await _create(client, host, _definition(enabled=False, triggers=[_one_time(run_at)]))
        assert await host.run_due_automations(run_at + timedelta(seconds=10)) == 0
        assert "nextRunAt" not in _entry(host)
    finally:
        await host.aclose()


async def test_fired_survives_a_restart(tmp_path: Path) -> None:
    run_at = datetime.now(UTC) + timedelta(hours=1)
    host = _host(FileAutomationStore(tmp_path))
    try:
        client, _ = await _client(host)
        await _create(client, host, _definition(triggers=[_one_time(run_at)]))
        assert await host.run_due_automations(run_at + timedelta(seconds=10)) == 1
        await _until(lambda: _runs(host)[0]["lifecycle"]["status"] == "completed")
    finally:
        await host.aclose()
    again = _host(FileAutomationStore(tmp_path))
    try:
        await _client(again)
        assert await again.run_due_automations(run_at + timedelta(minutes=1)) == 0
        assert "nextRunAt" not in _entry(again)
    finally:
        await again.aclose()


async def test_a_run_at_missed_while_the_host_was_down_catches_up(tmp_path: Path) -> None:
    run_at = datetime.now(UTC) + timedelta(hours=1)
    host = _host(FileAutomationStore(tmp_path))
    try:
        client, _ = await _client(host)
        await _create(client, host, _definition(triggers=[_one_time(run_at)]))
    finally:
        await host.aclose()
    again = _host(FileAutomationStore(tmp_path))
    try:
        await _client(again)
        assert _entry(again)["nextRunAt"] == iso(run_at)
        assert await again.run_due_automations(run_at + timedelta(hours=5)) == 1
        assert _runs(again)[0]["origin"]["catchUp"] is True
    finally:
        await again.aclose()
