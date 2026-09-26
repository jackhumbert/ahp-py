"""Keeping a node running: the supervisor, and what `install` registers."""

from __future__ import annotations

import plistlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from ahp_host.node.service import (
    Supervisor,
    launchd_plist,
    node_command,
    supervisor_log,
    task_xml,
)

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_a_child_that_exits_is_started_again_after_a_backoff(tmp_path: Path) -> None:
    log = tmp_path / "node.log.supervisor.log"
    supervisor = Supervisor([("node", [sys.executable, "-c", "raise SystemExit(3)"])], log)
    supervisor.poll_once(0.0)  # starts it
    (child,) = supervisor.children
    assert child.process is not None
    child.process.wait(timeout=10)
    supervisor.poll_once(1.0)  # notices the exit
    assert child.history == [3]
    assert child.process is None
    assert child.next_start == 1.0 + child.backoff()
    supervisor.poll_once(0.5 + child.next_start)  # due again: started
    assert child.process is not None
    child.process.wait(timeout=10)
    text = log.read_text()
    assert "node: started" in text
    assert "exited with 3" in text


def test_backoff_grows_while_failing_and_resets_after_a_healthy_run() -> None:
    supervisor = Supervisor([("node", ["unused"])], None)
    (child,) = supervisor.children
    child.failures = 1
    assert child.backoff() == 1
    child.failures = 4
    assert child.backoff() == 8
    child.failures = 30
    assert child.backoff() == 60


def test_a_command_that_cannot_start_is_retried_not_fatal(tmp_path: Path) -> None:
    log = tmp_path / "s.log"
    supervisor = Supervisor([("tunnel", [str(tmp_path / "no-such-program")])], log)
    supervisor.poll_once(0.0)
    (child,) = supervisor.children
    assert child.process is None
    assert child.failures == 1
    assert "could not start" in log.read_text()


def test_the_task_runs_the_supervisor_at_logon_and_keeps_it_up() -> None:
    command = node_command(
        Path(r"C:\Users\me\.config\ahp\node.toml"),
        Path(r"C:\Users\me\logs\node.log"),
        python=r"C:\venv\Scripts\pythonw.exe",
        verb="supervise",
    )
    xml = task_xml(command, user="STUDIO\\me", working_directory=r"C:\Users\me", description="d")
    root = ET.fromstring(xml.split("\n", 1)[1])  # without the UTF-16 declaration
    assert root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=_NS) == "STUDIO\\me"
    assert (
        root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=_NS) == "InteractiveToken"
    )
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=_NS) == "PT0S"
    assert root.findtext("t:Settings/t:RestartOnFailure/t:Count", namespaces=_NS) == "999"
    exec_ = root.find("t:Actions/t:Exec", _NS)
    assert exec_ is not None
    assert exec_.findtext("t:Command", namespaces=_NS) == r"C:\venv\Scripts\pythonw.exe"
    assert exec_.findtext("t:Arguments", namespaces=_NS) == (
        r"-m ahp_host.node supervise --config C:\Users\me\.config\ahp\node.toml"
        r" --log-file C:\Users\me\logs\node.log"
    )


def test_paths_with_spaces_are_quoted_in_the_task() -> None:
    xml = task_xml(
        ["pythonw.exe", "--config", r"C:\My Files\node.toml"],
        user="u",
        working_directory="w",
        description="d",
    )
    assert '--config "C:\\My Files\\node.toml"' in xml.replace("&quot;", '"')


def test_the_launchd_agent_keeps_the_supervisor_alive(tmp_path: Path) -> None:
    command = node_command(
        tmp_path / "node.toml", None, python="/venv/bin/python", verb="supervise"
    )
    spec = plistlib.loads(launchd_plist(command, label="io.ahp.node", log=tmp_path / "s.log"))
    assert spec["Label"] == "io.ahp.node"
    assert spec["KeepAlive"] is True
    assert spec["RunAtLoad"] is True
    assert spec["ProgramArguments"][:4] == [
        "/venv/bin/python",
        "-m",
        "ahp_host.node",
        "supervise",
    ]
    assert spec["StandardErrorPath"] == str(tmp_path / "s.log")


def test_the_supervisor_log_sits_beside_the_node_log(tmp_path: Path) -> None:
    assert supervisor_log(tmp_path / "node.log") == tmp_path / "node.log.supervisor.log"
    assert supervisor_log(None) is None
