"""`flow run` masks its journal, log lines and errors; resuming takes the script and --args.

Plan §8 M17 item 4. No Node: the scripts are Python.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahp_host_flow.cli import main
from ahp_host_flow.engine import load_journal, source_masked
from tests.flow_fakes import AWS, GH

SCRIPT = (
    f'# token = "{GH}"\n'
    "async def main(args):\n"
    "    log('using ' + args['key'])\n"
    "    if args.get('fail'):\n"
    "        raise ValueError('bad key ' + args['key'])\n"
    "    return await agent(args['k'])\n"
)
ARGS = json.dumps({"k": "hi", "key": AWS})


@pytest.fixture(autouse=True)
def _no_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ahp_host_flow.cli.find_node", lambda _node=None: None)


def _first(tmp_path: Path, *extra: str) -> tuple[Path, str]:
    script = tmp_path / "w.py"
    script.write_text(SCRIPT, encoding="utf-8")
    state = str(tmp_path / "state")
    argv = ["run", str(script), "--args", ARGS, "--state-dir", state, "--run-id", "r1", *extra]
    assert main(argv) == 0
    return script, state


def _journal(state: str, run_id: str) -> str:
    return (Path(state) / "runs" / run_id / "journal.jsonl").read_text(encoding="utf-8")


def test_cli_mask_journal_and_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, state = _first(tmp_path)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == "hi"
    assert "[log] using [redacted]" in captured.err
    assert AWS not in captured.err
    text = _journal(state, "r1")
    assert GH not in text
    assert AWS not in text
    assert source_masked(load_journal(Path(state), "r1").header)


def test_cli_mask_resume_with_script_and_args(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script, state = _first(tmp_path)
    capsys.readouterr()
    resume = ["run", str(script), "--state-dir", state, "--resume", "r1", "--run-id", "r2"]
    assert main([*resume, "--args", ARGS]) == 0
    assert json.loads(capsys.readouterr().out) == "hi"
    agents = load_journal(Path(state), "r2").agents
    assert [a.status for a in agents] == ["ok"]
    assert AWS not in _journal(state, "r2")


def test_cli_mask_resume_refuses_other_args_and_no_args(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script, state = _first(tmp_path)
    capsys.readouterr()
    resume = ["run", str(script), "--state-dir", state, "--resume", "r1"]
    other = json.dumps({"k": "hi", "key": "other"})
    assert main([*resume, "--run-id", "r2", "--args", other]) == 1
    assert "the args differ" in capsys.readouterr().err
    assert main([*resume, "--run-id", "r3"]) == 1
    assert "holds masked args" in capsys.readouterr().err
    assert main(["run", "--state-dir", state, "--resume", "r1", "--run-id", "r4"]) == 1
    assert "holds a masked copy of the script" in capsys.readouterr().err


def test_cli_mask_keep_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, state = _first(tmp_path, "--keep-source")
    capsys.readouterr()
    text = _journal(state, "r1")
    assert GH in text
    assert AWS in text
    # Kept as it is, so the journal alone resumes it, as before M17.
    assert main(["run", "--state-dir", state, "--resume", "r1", "--run-id", "r2"]) == 0
    assert json.loads(capsys.readouterr().out) == "hi"


def test_cli_mask_run_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = tmp_path / "w.py"
    script.write_text(SCRIPT, encoding="utf-8")
    failing = json.dumps({"k": "hi", "key": AWS, "fail": True})
    assert main(["run", str(script), "--args", failing]) == 1
    err = capsys.readouterr().err
    assert "flow: " in err
    assert "bad key [redacted]" in err
    assert AWS not in err


def test_cli_mask_wire_log_needs_config(tmp_path: Path) -> None:
    script = tmp_path / "w.py"
    script.write_text(SCRIPT, encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        main(["run", str(script), "--wire-log", str(tmp_path / "w.jsonl")])
    assert info.value.code == 2
