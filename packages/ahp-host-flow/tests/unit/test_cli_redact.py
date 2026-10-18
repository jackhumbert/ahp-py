"""`flow redact`: the journal-only mode, the request spool mode and the server's poller."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from ahp_host import Host, LoopbackSingleUserPolicy

from ahp_host_flow import FlowProvider, load_config, redaction
from ahp_host_flow.__main__ import build_host
from ahp_host_flow.cli import main
from ahp_host_flow.engine import (
    JOURNAL_FILE,
    EchoDispatcher,
    RunOptions,
    run_dir,
)
from ahp_host_flow.library import run

SECRET = "PLAINLEAK-4242"

ECHO_TOML = """\
state_dir = "state"

[providers.echo]
load = "ahp_host.provider.echo:EchoProvider"
"""


async def _finished_run(state_dir: Path) -> str:
    async with run(EchoDispatcher(), RunOptions(state_dir=state_dir)) as f:
        await f.agent("say " + SECRET)
        assert f.run_id is not None
        return f.run_id


def _journal(state_dir: Path, run_id: str) -> str:
    return (run_dir(state_dir, run_id) / JOURNAL_FILE).read_text(encoding="utf-8")


def test_cli_redact_state_dir_rewrites_the_journal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = asyncio.run(_finished_run(tmp_path))
    assert SECRET in _journal(tmp_path, run_id)

    code = main(["redact", run_id, "0", "--state-dir", str(tmp_path), "--reason", "leak"])

    assert code == 0
    assert SECRET not in _journal(tmp_path, run_id)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["runId"] == run_id
    assert out["journal"] is True
    assert out["sharedView"] is False


def test_cli_redact_refuses_an_unknown_ordinal_and_a_stray_wait(tmp_path: Path) -> None:
    run_id = asyncio.run(_finished_run(tmp_path))
    assert main(["redact", run_id, "9", "--state-dir", str(tmp_path)]) == 1
    with pytest.raises(SystemExit) as info:
        main(["redact", run_id, "0", "--state-dir", str(tmp_path), "--wait", "1"])
    assert info.value.code == 2


def test_cli_redact_without_a_host_stays_queued(tmp_path: Path) -> None:
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")

    code = main(["redact", "run-1", "0", "--config", str(toml), "--wait", "0", "--reason", "why"])

    assert code == 3
    requests = list((tmp_path / "state" / "redact").glob("*.request.json"))
    assert len(requests) == 1
    assert json.loads(requests[0].read_text(encoding="utf-8")) == {
        "runId": "run-1",
        "ordinal": 0,
        "reason": "why",
        "actor": redaction.local_actor(),
    }


def test_cli_redact_through_a_host_that_answers(tmp_path: Path) -> None:
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    config = load_config(toml)
    run_id = asyncio.run(_finished_run(config.state_dir))
    codes: list[int] = []
    thread = threading.Thread(
        target=lambda: codes.append(
            main(["redact", run_id, "0", "--config", str(toml), "--wait", "5"])
        )
    )
    thread.start()
    spool = config.state_dir / redaction.REQUESTS_DIR
    deadline = time.monotonic() + 5
    while not list(spool.glob("*" + redaction.REQUEST_SUFFIX)) and time.monotonic() < deadline:
        time.sleep(0.05)

    provider = FlowProvider(config)
    host = Host(provider, LoopbackSingleUserPolicy())
    asyncio.run(redaction.process_requests(host, provider))
    thread.join(timeout=10)

    assert codes == [0]
    assert SECRET not in _journal(config.state_dir, run_id)


async def test_cli_redact_the_server_starts_a_poller(tmp_path: Path) -> None:
    toml = tmp_path / "flow.toml"
    toml.write_text(ECHO_TOML, encoding="utf-8")
    poller = redaction.start_poller(build_host(load_config(toml)))
    assert isinstance(poller, asyncio.Task)
    poller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poller


def test_cli_redact_state_dir_refuses_a_live_writer(tmp_path: Path) -> None:
    from ahp_host_flow.engine import AgentRecord, JournalWriter, RunHeader

    writer = JournalWriter.create(tmp_path, RunHeader("live2", "x.js", "s", False, None, 10, None))
    writer.append_agent(AgentRecord(0, "k", "ok", SECRET))
    try:
        assert main(["redact", "live2", "0", "--state-dir", str(tmp_path)]) == 1
        assert SECRET in _journal(tmp_path, "live2")
    finally:
        writer.close()
    assert main(["redact", "live2", "0", "--state-dir", str(tmp_path)]) == 0
    assert SECRET not in _journal(tmp_path, "live2")
