"""A secret in a run's args and script text reaches neither the journal nor the wire log.

The proving test of plan §8 M17 item 4: the flow host is built as `python -m ahp_host_flow`
builds it (`build_host`, with `--wire-log`), so its wire log is masked by the provider's
`Redactor` beside ahp-host's credential redaction. Also `[journal] keep_source` on the
provider path, and `flow run --config --wire-log`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahp_host_flow.__main__ import _parse_args, build_host
from ahp_host_flow.cli import main as flow_main
from ahp_host_flow.config import load_config
from ahp_host_flow.engine import load_journal, source_masked
from tests.flow_fakes import AWS, GH
from tests.flow_hosting import ECHO, SESSION, final_json, write_toml
from tests.hosting import connect, open_session, run_turn, shut

WORKFLOW = (
    "meta = {'name': 'masked', 'description': 'd'}\n"
    f'# deploy token = "{GH}"\n'
    "\n"
    "async def main(args):\n"
    "    log('key is ' + args['key'])\n"
    "    return await agent('one ' + args['k'])\n"
)
COMMAND = "/masked " + json.dumps({"k": "hi", "key": AWS})
TOML = (
    f'[providers.echo]\nload = "{ECHO}"\n\n[routing]\ndefault = "echo"\n\n'
    # A workspace .py workflow is trusted here: this test is its operator.
    '[workflows]\npython_sources = ["workspace", "user", "host"]\n'
)


def _journals(base: Path) -> str:
    runs = base / "state" / "runs"
    return "".join(p.read_text(encoding="utf-8") for p in sorted(runs.glob("*/journal.jsonl")))


async def _hosted_run(base: Path, body: str, wire_log: Path | None) -> dict[str, object]:
    workspace = base / "ws"
    flows = workspace / ".claude" / "workflows"
    flows.mkdir(parents=True)
    (flows / "masked.py").write_text(WORKFLOW, encoding="utf-8")
    config = load_config(write_toml(base, str(base / "no-node"), body))
    host = build_host(config, store=False, wire_log=wire_log)
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, SESSION, "flow", working_directories=[workspace.as_uri()])
        turn = await run_turn(wire, chat, "t1", COMMAND)
        assert turn["state"] == "complete", turn
        return turn
    finally:
        await shut(host, wire, serving)


async def test_maskwire_secret_in_neither_journal_nor_wire_log(tmp_path: Path) -> None:
    log = tmp_path / "agent-host-flow.jsonl"
    turn = await _hosted_run(tmp_path, TOML, log)
    assert final_json(turn) == "You said: one hi"
    journal = _journals(tmp_path)
    assert '"sourceSha"' in journal
    assert GH not in journal
    assert AWS not in journal
    frames = log.read_text(encoding="utf-8")
    # The log saw the run: the start message, the log() line and the reply.
    assert "/masked" in frames
    assert "key is [redacted]" in frames
    assert "You said: one hi" in frames
    assert GH not in frames
    assert AWS not in frames


async def test_maskwire_keep_source_keeps_the_text(tmp_path: Path) -> None:
    await _hosted_run(tmp_path, TOML + "\n[journal]\nkeep_source = true\n", None)
    journal = _journals(tmp_path)
    assert GH in journal
    assert AWS in journal
    (run,) = (tmp_path / "state" / "runs").iterdir()
    assert not source_masked(load_journal(tmp_path / "state", run.name).header)


def test_maskwire_main_takes_wire_log(tmp_path: Path) -> None:
    args = _parse_args(["--config", "flow.toml", "--wire-log", str(tmp_path / "w.jsonl")])
    assert args.wire_log == tmp_path / "w.jsonl"


@pytest.mark.parametrize("keep", [False, True])
def test_maskwire_flow_run_config_wire_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], keep: bool
) -> None:
    toml = write_toml(tmp_path, str(tmp_path / "no-node"), TOML)
    script = tmp_path / "masked.py"
    script.write_text(WORKFLOW, encoding="utf-8")
    log = tmp_path / "agent-host-run.jsonl"
    argv = [
        "run",
        str(script),
        "--config",
        str(toml),
        "--root",
        str(tmp_path),
        "--args",
        json.dumps({"k": "hi", "key": AWS}),
        "--wire-log",
        str(log),
    ]
    assert flow_main([*argv, "--keep-source"] if keep else argv) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == "You said: one hi"
    assert AWS not in captured.err
    frames = log.read_text(encoding="utf-8")
    assert "You said: one hi" in frames
    assert GH not in frames
    assert AWS not in frames
    journal = _journals(tmp_path)
    assert (GH in journal) is keep
    assert (AWS in journal) is keep


async def test_maskwire_run_error_masked_on_the_provider_path(tmp_path: Path) -> None:
    """A script's error that quotes its args fails the turn masked, in the log as well."""
    workspace = tmp_path / "ws"
    flows = workspace / ".claude" / "workflows"
    flows.mkdir(parents=True)
    (flows / "masked.py").write_text(
        "meta = {'name': 'masked', 'description': 'd'}\n"
        "\n"
        "async def main(args):\n"
        "    raise ValueError('bad key ' + args['key'])\n",
        encoding="utf-8",
    )
    log = tmp_path / "agent-host-flow.jsonl"
    config = load_config(write_toml(tmp_path, str(tmp_path / "no-node"), TOML))
    host = build_host(config, store=False, wire_log=log)
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, SESSION, "flow", working_directories=[workspace.as_uri()])
        turn = await run_turn(wire, chat, "t1", COMMAND)
    finally:
        await shut(host, wire, serving)
    assert turn["state"] == "error"
    text = json.dumps(turn["responseParts"])
    assert "bad key [redacted]" in text
    assert AWS not in text
    assert AWS not in log.read_text(encoding="utf-8")
