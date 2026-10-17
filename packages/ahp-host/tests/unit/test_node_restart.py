"""Graceful restarts: the node's request files, its drain-then-close, and its config."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest

from ahp_host.node.config import DEFAULT_DRAIN_TIMEOUT, ConfigError, load
from ahp_host.node.runner import (
    RESTART_REQUEST,
    Shutdown,
    ShutdownRequest,
    _parse_args,
    check_request_files,
    clear_request_files,
    main,
    read_request_timeout,
    watch_request_files,
    wind_down,
    write_restart_request,
)
from ahp_host.node.service import RESTART_EXIT_CODE, STOP_REQUEST, published_drain_timeout


class _FakeHost:
    def __init__(self, still_running: list[str] | None = None) -> None:
        self.calls: list[tuple[str, float | None]] = []
        self._still_running = still_running or []

    async def drain(self, timeout: float) -> list[str]:
        self.calls.append(("drain", timeout))
        return self._still_running

    async def aclose(self) -> None:
        self.calls.append(("aclose", None))


def _config(tmp_path: Path, body: str = "") -> argparse.Namespace:
    (tmp_path / "work").mkdir(exist_ok=True)
    config = tmp_path / "node.toml"
    config.write_text(
        f"root = '{tmp_path / 'work'}'\nstate_dir = '{tmp_path / 'state'}'\n"
        f"{body}\n[[agents]]\ntype = 'x'\n"
    )
    return argparse.Namespace(
        config=config, root=None, port=None, bind=None, token_file=None,
        state_dir=None, log_file=None, verbose=None,
    )  # fmt: skip


def test_drain_timeout_defaults_to_five_minutes_and_can_be_set(tmp_path: Path) -> None:
    assert load(_config(tmp_path)).drain_timeout == DEFAULT_DRAIN_TIMEOUT == 300.0
    assert load(_config(tmp_path, "drain_timeout = 45")).drain_timeout == 45.0
    assert load(_config(tmp_path, "drain_timeout = 0.5")).drain_timeout == 0.5


@pytest.mark.parametrize("value", ['"soon"', "-1", "true"])
def test_drain_timeout_must_be_a_non_negative_number(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="drain_timeout"):
        load(_config(tmp_path, f"drain_timeout = {value}"))


def test_no_request_file_means_no_request(tmp_path: Path) -> None:
    assert check_request_files(tmp_path) is None
    assert check_request_files(tmp_path / "missing") is None


def test_a_restart_request_carries_an_optional_timeout(tmp_path: Path) -> None:
    write_restart_request(tmp_path)
    assert check_request_files(tmp_path) == ShutdownRequest("restart", None)
    write_restart_request(tmp_path, 12.5)
    assert check_request_files(tmp_path) == ShutdownRequest("restart", 12.5)


def test_a_stop_request_outranks_a_restart_request(tmp_path: Path) -> None:
    write_restart_request(tmp_path)
    (tmp_path / STOP_REQUEST).write_text("{}")
    assert check_request_files(tmp_path) == ShutdownRequest("stop", None)


@pytest.mark.parametrize("body", ["", "not json", "[1]", '{"timeout": "x"}', '{"timeout": -3}'])
def test_an_unreadable_request_file_still_counts_and_uses_the_default(
    tmp_path: Path, body: str
) -> None:
    path = tmp_path / RESTART_REQUEST
    path.write_text(body)
    assert read_request_timeout(path) is None
    assert check_request_files(tmp_path) == ShutdownRequest("restart", None)


def test_the_timeout_in_a_request_file_is_read(tmp_path: Path) -> None:
    path = tmp_path / RESTART_REQUEST
    path.write_text(json.dumps({"timeout": 7}))
    assert read_request_timeout(path) == 7.0


def test_clearing_removes_both_files_and_tolerates_none(tmp_path: Path) -> None:
    write_restart_request(tmp_path)
    (tmp_path / STOP_REQUEST).write_text("{}")
    clear_request_files(tmp_path)
    clear_request_files(tmp_path)
    assert check_request_files(tmp_path) is None


@pytest.mark.anyio
async def test_the_watcher_reports_a_file_that_appears_later(tmp_path: Path) -> None:
    seen: list[ShutdownRequest] = []
    task = asyncio.create_task(watch_request_files(tmp_path, seen.append, interval=0.01))
    await asyncio.sleep(0.05)
    assert not seen
    write_restart_request(tmp_path, 3)
    await asyncio.wait_for(task, timeout=5)
    assert seen == [ShutdownRequest("restart", 3.0)]


@pytest.mark.anyio
async def test_a_restart_drains_then_closes_then_asks_for_exit_75(tmp_path: Path) -> None:
    host = _FakeHost()
    write_restart_request(tmp_path)
    code = await wind_down(host, ShutdownRequest("restart"), 300.0, tmp_path)
    assert code == RESTART_EXIT_CODE == 75
    assert host.calls == [("drain", 300.0), ("aclose", None)]
    assert check_request_files(tmp_path) is None  # deleted, or it would loop


@pytest.mark.anyio
async def test_a_stop_exits_zero_and_a_request_timeout_overrides_the_config(
    tmp_path: Path,
) -> None:
    host = _FakeHost(still_running=["ahp://chat/1"])
    code = await wind_down(host, ShutdownRequest("stop", 2.0), 300.0, tmp_path)
    assert code == 0
    assert host.calls == [("drain", 2.0), ("aclose", None)]


def test_the_restart_verb_writes_the_request_and_says_what_happens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _config(tmp_path)
    assert _parse_args(["restart", "--config", str(args.config), "--timeout", "9"]).timeout == 9
    config = args.config.read_text().replace("[[agents]]", "[[agents]]")
    args.config.write_text(config)
    main(["restart", "--config", str(args.config), "--timeout", "9"])
    assert check_request_files(tmp_path / "state") == ShutdownRequest("restart", 9.0)
    out = capsys.readouterr().out
    assert "restart once its turns finish" in out
    assert "9s" in out


@pytest.mark.anyio
async def test_a_watcher_that_keeps_going_passes_on_each_file_once(tmp_path: Path) -> None:
    seen: list[ShutdownRequest] = []
    task = asyncio.create_task(
        watch_request_files(tmp_path, seen.append, interval=0.01, once=False)
    )
    try:
        write_restart_request(tmp_path, 3)
        await asyncio.sleep(0.1)
        assert seen == [ShutdownRequest("restart", 3.0)]  # not once per poll
        (tmp_path / STOP_REQUEST).write_text("{}")
        await asyncio.sleep(0.1)
        assert seen[-1] == ShutdownRequest("stop", None)
        assert len(seen) == 2
        assert not task.done()
    finally:
        task.cancel()


@pytest.mark.anyio
async def test_a_stop_during_a_restart_drain_turns_the_exit_into_a_stop(tmp_path: Path) -> None:
    shutdown = Shutdown()
    shutdown.ask(ShutdownRequest("restart"))
    shutdown.ask(ShutdownRequest("stop"))
    assert shutdown.request == ShutdownRequest("stop", None)
    assert not shutdown.cut_short.is_set(), "the drain carries on to its own deadline"
    shutdown.ask(ShutdownRequest("restart"))
    assert shutdown.request is not None
    assert shutdown.request.kind == "stop", "a restart never undoes a stop"


@pytest.mark.anyio
async def test_a_second_stop_cuts_a_stop_drain_short() -> None:
    shutdown = Shutdown()
    shutdown.ask(ShutdownRequest("stop"))
    assert not shutdown.cut_short.is_set()
    shutdown.ask(ShutdownRequest("stop"))
    assert shutdown.cut_short.is_set()


@pytest.mark.anyio
async def test_a_second_stop_ends_the_wait_and_still_closes_the_host(tmp_path: Path) -> None:
    release = asyncio.Event()

    class Slow(_FakeHost):
        async def drain(self, timeout: float) -> list[str]:
            self.calls.append(("drain", timeout))
            if timeout > 0:
                await release.wait()  # a turn that never ends
            return ["ahp://chat/1"]

    host = Slow()
    shutdown = Shutdown()
    shutdown.ask(ShutdownRequest("stop"))
    winding = asyncio.create_task(
        wind_down(host, ShutdownRequest("stop"), 300.0, tmp_path, shutdown)
    )
    await asyncio.sleep(0.05)
    assert not winding.done()
    shutdown.ask(ShutdownRequest("stop"))
    assert await asyncio.wait_for(winding, 5) == 0
    # The wait was abandoned, then the zero-length drain recorded what was left.
    assert host.calls == [("drain", 300.0), ("drain", 0.0), ("aclose", None)]


@pytest.mark.anyio
async def test_a_stop_that_arrives_mid_restart_drain_exits_zero(tmp_path: Path) -> None:
    release = asyncio.Event()

    class Slow(_FakeHost):
        async def drain(self, timeout: float) -> list[str]:
            await release.wait()
            return []

    shutdown = Shutdown()
    shutdown.ask(ShutdownRequest("restart"))
    winding = asyncio.create_task(
        wind_down(Slow(), ShutdownRequest("restart"), 300.0, tmp_path, shutdown)
    )
    await asyncio.sleep(0.05)
    shutdown.ask(ShutdownRequest("stop"))
    await asyncio.sleep(0.05)
    assert not winding.done(), "a stop after a restart drain still lets the turns finish"
    release.set()
    assert await asyncio.wait_for(winding, 5) == 0


@pytest.mark.anyio
async def test_winding_down_publishes_the_timeout_it_is_waiting_for(tmp_path: Path) -> None:
    await wind_down(_FakeHost(), ShutdownRequest("restart", 900.0), 300.0, tmp_path)
    assert published_drain_timeout(tmp_path) == 900.0
