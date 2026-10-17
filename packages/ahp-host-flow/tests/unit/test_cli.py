"""The `flow run` command."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ahp_host_flow.cli import main

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
