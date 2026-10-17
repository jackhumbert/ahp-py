"""The `flow run` command."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ahp_host_flow.cli import main
from ahp_host_flow.engine import load_journal

HELLO = """\
export const meta = { name: 'hello', description: 'one echo agent call' }
const reply = await agent(`hello ${args.who}`)
return { reply }
"""


def _write(tmp_path: Path, source: str, name: str = "script.js") -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


@pytest.mark.node
def test_cli_run_prints_result(
    node_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _write(tmp_path, HELLO, "hello.js")
    code = main(["run", str(script), "--args", '{"who": "world"}', "--node", node_path])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"reply": "hello world"}


@pytest.mark.node
def test_cli_run_as_module(node_path: str, tmp_path: Path) -> None:
    script = _write(tmp_path, HELLO, "hello.js")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ahp_host_flow.cli",
            "run",
            str(script),
            "--args",
            '{"who": "world"}',
            "--node",
            node_path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"reply": "hello world"}


@pytest.mark.node
def test_cli_run_progress_on_stderr(
    node_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _write(tmp_path, "phase('A'); log('x'); return 1")
    assert main(["run", str(script), "--node", node_path]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "1"
    assert "[phase] A" in captured.err
    assert "[log] x" in captured.err


@pytest.mark.node
def test_cli_run_script_error(
    node_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _write(tmp_path, "throw new Error('boom')")
    assert main(["run", str(script), "--node", node_path]) == 1
    assert "flow: boom" in capsys.readouterr().err


def test_cli_run_bad_args_json(tmp_path: Path) -> None:
    script = _write(tmp_path, "return 1")
    with pytest.raises(SystemExit) as info:
        main(["run", str(script), "--args", "{nope"])
    assert info.value.code == 2


def test_cli_run_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", str(tmp_path / "absent.js")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_cli_run_no_node(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _write(tmp_path, "return 1")
    code = main(["run", str(script), "--node", str(tmp_path / "missing-node")])
    assert code == 1
    assert "Node >= 20" in capsys.readouterr().err


@pytest.mark.node
def test_cli_run_budget_zero(
    node_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _write(tmp_path, "try { await agent('x') } catch (e) { return e.message }")
    assert main(["run", str(script), "--budget", "0", "--node", node_path]) == 0
    assert json.loads(capsys.readouterr().out) == "budget exhausted: 0 of 0 output tokens spent"


@pytest.mark.node
def test_cli_run_timeout(
    node_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _write(tmp_path, "while (true) {}")
    assert main(["run", str(script), "--timeout", "0.5", "--node", node_path]) == 1
    assert "timed out" in capsys.readouterr().err


@pytest.mark.node
def test_cli_run_workflows_dir(
    node_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wf = tmp_path / "wf"
    wf.mkdir()
    (wf / "child.js").write_text("return 'child ' + args.who", encoding="utf-8")
    by_name = _write(tmp_path, "return await workflow('child', {who: 'cli'})", "by_name.js")
    assert main(["run", str(by_name), "--workflows", str(wf), "--node", node_path]) == 0
    assert json.loads(capsys.readouterr().out) == "child cli"

    by_path = _write(
        tmp_path,
        "return await workflow({scriptPath: 'wf/child.js'}, {who: 'root'})",
        "by_path.js",
    )
    assert main(["run", str(by_path), "--root", str(tmp_path), "--node", node_path]) == 0
    assert json.loads(capsys.readouterr().out) == "child root"


def test_cli_run_bad_concurrency(tmp_path: Path) -> None:
    script = _write(tmp_path, "return 1")
    with pytest.raises(SystemExit) as info:
        main(["run", str(script), "--concurrency", "0"])
    assert info.value.code == 2


@pytest.mark.node
def test_cli_run_state_dir_prints_run_id(
    node_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _write(tmp_path, HELLO, "hello.js")
    state = tmp_path / "state"
    argv = ["run", str(script), "--args", '{"who": "world"}', "--state-dir", str(state)]
    assert main([*argv, "--run-id", "r1", "--node", node_path]) == 0
    assert "[run] r1" in capsys.readouterr().err
    assert (state / "runs" / "r1" / "journal.jsonl").is_file()


@pytest.mark.node
def test_cli_resume(node_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _write(tmp_path, HELLO, "hello.js")
    state = str(tmp_path / "state")
    first = ["run", str(script), "--args", '{"who": "world"}', "--state-dir", state]
    assert main([*first, "--run-id", "r1", "--node", node_path]) == 0
    expected = capsys.readouterr().out

    resume = ["run", "--state-dir", state, "--resume", "r1", "--run-id", "r2", "--node", node_path]
    assert main(resume) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == json.loads(expected)
    assert "[run] r2" in captured.err
    agents = load_journal(Path(state), "r2").agents
    assert len(agents) == 1
    assert agents[0].status == "ok"


@pytest.mark.node
def test_cli_resume_edited_script(
    node_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _write(tmp_path, HELLO, "hello.js")
    state = str(tmp_path / "state")
    first = ["run", str(script), "--args", '{"who": "world"}', "--state-dir", state]
    assert main([*first, "--run-id", "r1", "--node", node_path]) == 0
    capsys.readouterr()

    edited = _write(tmp_path, HELLO.replace("hello ${", "hi ${"), "edited.js")
    resume = ["run", str(edited), "--state-dir", state, "--resume", "r1", "--node", node_path]
    assert main(resume) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"reply": "hi world"}
    assert "[log] resume: cache ended at agent() #0" in captured.err


@pytest.mark.node
def test_cli_resume_unknown_run(
    node_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    code = main(["run", "--state-dir", str(state), "--resume", "nope", "--node", node_path])
    assert code == 1
    assert "flow: no journal for run 'nope'" in capsys.readouterr().err


def test_cli_run_needs_path_or_resume() -> None:
    with pytest.raises(SystemExit) as info:
        main(["run"])
    assert info.value.code == 2


def test_cli_resume_rejects_args(tmp_path: Path) -> None:
    argv = ["run", "--resume", "r1", "--state-dir", str(tmp_path), "--args", "{}"]
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == 2


def test_cli_resume_needs_state_dir() -> None:
    with pytest.raises(SystemExit) as info:
        main(["run", "--resume", "r1"])
    assert info.value.code == 2
