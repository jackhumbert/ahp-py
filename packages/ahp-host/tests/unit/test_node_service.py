"""Keeping a node running: the supervisor, and what `install` registers."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ahp_host.node.service import (
    RESTART_EXIT_CODE,
    Supervisor,
    install_linux,
    launchd_plist,
    node_command,
    publish_drain_timeout,
    published_drain_timeout,
    supervisor_log,
    systemd_path,
    systemd_unit,
    task_xml,
    uninstall_linux,
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
    # Not Task Scheduler's default of 7, which also lowers I/O priority.
    assert root.findtext("t:Settings/t:Priority", namespaces=_NS) == "4"
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


def _unit_lines(unit: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)


def test_the_systemd_unit_runs_the_supervisor_and_keeps_it_up(tmp_path: Path) -> None:
    home = tmp_path / "home"
    command = node_command(
        home / ".config/ahp/node.toml", None, python="/venv/bin/python", verb="supervise"
    )
    unit = systemd_unit(
        command, home=home, path=systemd_path(home, "/usr/bin:/bin"), description="ahp-node"
    )
    lines = _unit_lines(unit)
    assert lines["ExecStart"] == (
        f"/venv/bin/python -m ahp_host.node supervise --config {home}/.config/ahp/node.toml"
    )
    assert lines["Restart"] == "always"
    assert lines["RestartSec"] == "10"
    assert lines["WantedBy"] == "default.target"
    assert lines["After"] == "network-online.target"
    assert lines["Wants"] == "network-online.target"
    assert lines["WorkingDirectory"] == str(home)
    assert lines["Environment"] == f"PATH={home}/.local/bin:{home}/bin:/usr/bin:/bin"


def test_the_systemd_unit_quotes_spaces_and_escapes_specifiers() -> None:
    unit = systemd_unit(
        ["/opt/my env/bin/python", "50%", "$HOME"],
        home=Path("/home/a b"),
        path="/home/a b/.local/bin:/usr/bin",
        description="ahp-node",
    )
    lines = _unit_lines(unit)
    assert lines["ExecStart"] == '"/opt/my env/bin/python" 50%% $$HOME'
    assert lines["Environment"] == '"PATH=/home/a b/.local/bin:/usr/bin"'


def test_the_systemd_path_falls_back_to_a_stock_one_without_duplicates() -> None:
    home = Path("/home/u")
    assert systemd_path(home, None).startswith("/home/u/.local/bin:/home/u/bin:/usr/local/sbin")
    assert systemd_path(home, "/home/u/.local/bin:/usr/bin") == (
        "/home/u/.local/bin:/home/u/bin:/usr/bin"
    )


class _FakeSystemd:
    """Records each command; `fail` maps a command's first words to an error."""

    def __init__(self, fail: dict[tuple[str, ...], str] | None = None, linger: str = "no") -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or {}
        self.linger = linger

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        for prefix, error in self.fail.items():
            if tuple(command[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(command, 1, "", error)
        stdout = self.linger + "\n" if "show-user" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")


def test_install_linux_writes_the_unit_lingers_and_restarts_it(tmp_path: Path) -> None:
    fake = _FakeSystemd()
    unit_path = tmp_path / "systemd/user/ahp-node.service"
    message = install_linux(
        ["/venv/bin/python", "-m", "ahp_host.node", "supervise"],
        "ahp-node.service",
        run=fake,
        unit_path=unit_path,
        user="me",
    )
    assert "ExecStart=/venv/bin/python -m ahp_host.node supervise" in unit_path.read_text()
    assert fake.calls[1] == ["loginctl", "enable-linger", "me"]
    assert fake.calls[2:] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "ahp-node.service"],
        ["systemctl", "--user", "restart", "ahp-node.service"],
    ]
    assert "lingering turned on" in message


def test_install_linux_says_plainly_when_lingering_needs_sudo(tmp_path: Path) -> None:
    fake = _FakeSystemd(fail={("loginctl", "enable-linger"): "Access denied"})
    message = install_linux(
        ["/venv/bin/python"],
        "ahp-node",
        run=fake,
        unit_path=tmp_path / "ahp-node.service",
        user="me",
    )
    assert "Access denied" in message
    assert "sudo loginctl enable-linger me" in message
    assert ["systemctl", "--user", "restart", "ahp-node.service"] in fake.calls


def test_install_linux_skips_lingering_already_on(tmp_path: Path) -> None:
    fake = _FakeSystemd(linger="yes")
    message = install_linux(
        ["/venv/bin/python"], "ahp-node", run=fake, unit_path=tmp_path / "u", user="me"
    )
    assert ["loginctl", "enable-linger", "me"] not in fake.calls
    assert "already on" in message


def test_install_linux_fails_loudly_when_systemctl_does(tmp_path: Path) -> None:
    fake = _FakeSystemd(fail={("systemctl", "--user", "daemon-reload"): "no user bus"})
    with pytest.raises(SystemExit, match="no user bus"):
        install_linux(["/x"], "ahp-node", run=fake, unit_path=tmp_path / "u", user="me")


def test_uninstall_linux_disables_removes_and_reloads(tmp_path: Path) -> None:
    unit_path = tmp_path / "ahp-node.service"
    unit_path.write_text("[Unit]\n")
    fake = _FakeSystemd()
    assert "removed" in uninstall_linux("ahp-node", run=fake, unit_path=unit_path)
    assert not unit_path.exists()
    assert fake.calls == [
        ["systemctl", "--user", "disable", "--now", "ahp-node.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not any(call[0] == "loginctl" for call in fake.calls)
    assert "no systemd user unit" in uninstall_linux("ahp-node", run=fake, unit_path=unit_path)


def test_a_child_asking_for_a_restart_comes_back_at_once_with_failures_forgotten() -> None:
    supervisor = Supervisor(
        [("node", [sys.executable, "-c", f"raise SystemExit({RESTART_EXIT_CODE})"])], None
    )
    (child,) = supervisor.children
    supervisor.poll_once(0.0)
    assert child.process is not None
    child.process.wait(timeout=10)
    child.failures = 5
    supervisor.poll_once(1.0)
    assert child.history == [75]
    assert child.failures == 0
    assert child.next_start == 1.0  # no backoff
    supervisor.poll_once(1.0)
    assert child.process is not None  # started again on the same tick
    child.process.wait(timeout=10)


class _FakeProcess:
    def __init__(self, exits_on: str | None) -> None:
        self.exits_on = exits_on
        self.events: list[str] = []
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.events.append("terminate")
        if self.exits_on == "terminate":
            self.code = 0

    def kill(self) -> None:
        self.events.append("kill")
        self.code = -9


def _supervising(
    process: _FakeProcess, tmp_path: Path, *, windows: bool, timeout: float = 0.0
) -> Supervisor:
    supervisor = Supervisor([("node", ["unused"])], None, state_dir=tmp_path, drain_timeout=timeout)
    supervisor._windows = windows
    supervisor.children[0].process = process  # type: ignore[assignment]
    return supervisor


def test_stopping_on_posix_sends_sigterm_and_lets_the_node_drain(tmp_path: Path) -> None:
    process = _FakeProcess(exits_on="terminate")
    _supervising(process, tmp_path, windows=False)._stop_children(sleep=lambda _: None)
    assert process.events == ["terminate"]  # no kill: it exited on its own


def test_stopping_kills_a_node_that_outlasts_the_drain_wait(tmp_path: Path) -> None:
    process = _FakeProcess(exits_on=None)
    supervisor = _supervising(process, tmp_path, windows=False, timeout=-100.0)
    supervisor._stop_children(sleep=lambda _: None)
    assert process.events == ["terminate", "kill"]


def test_stopping_on_windows_asks_by_file_first_and_terminates_last(tmp_path: Path) -> None:
    process = _FakeProcess(exits_on=None)
    supervisor = _supervising(process, tmp_path, windows=True, timeout=-100.0)
    supervisor._stop_children(sleep=lambda _: None)
    assert (tmp_path / "stop-request").exists()
    assert process.events == ["kill"]  # never terminated before the wait ended


def test_the_launchd_agent_waits_out_a_drain_before_killing(tmp_path: Path) -> None:
    spec = plistlib.loads(launchd_plist(["x"], label="l", log=None, drain_timeout=120))
    assert spec["ExitTimeOut"] == 150


def test_the_systemd_unit_signals_only_the_supervisor_and_waits_for_the_drain() -> None:
    unit = systemd_unit(
        ["x"], home=Path("/home/u"), path="/usr/bin", description="d", drain_timeout=120
    )
    lines = _unit_lines(unit)
    assert lines["KillMode"] == "mixed"
    assert lines["TimeoutStopSec"] == "150"


class _TwoChildren(_FakeProcess):
    """A process that notes, in a shared log, what was done to it and when."""

    def __init__(self, name: str, log: list[str], *, exits_on: str | None = "terminate") -> None:
        super().__init__(exits_on)
        self.name = name
        self.log = log

    def terminate(self) -> None:
        self.log.append(f"{self.name}:terminate")
        super().terminate()

    def kill(self) -> None:
        self.log.append(f"{self.name}:kill")
        super().kill()


def test_the_tunnel_outlives_the_nodes_drain(tmp_path: Path) -> None:
    log: list[str] = []
    node = _TwoChildren("node", log, exits_on=None)
    tunnel = _TwoChildren("tunnel", log)
    supervisor = Supervisor(
        [("node", ["unused"]), ("tunnel", ["unused"])], None, state_dir=tmp_path, drain_timeout=0.0
    )
    supervisor._windows = False
    supervisor.children[0].process = node  # type: ignore[assignment]
    supervisor.children[1].process = tunnel  # type: ignore[assignment]

    def sleep(_: float) -> None:
        # The node finishes draining a moment after it was asked.
        if "node:terminate" in log and node.code is None and "tunnel:terminate" not in log:
            node.code = 0
            log.append("node:exited")

    supervisor._stop_children(sleep=sleep)
    assert log == ["node:terminate", "node:exited", "tunnel:terminate"]


def test_the_supervisor_waits_for_the_drain_the_node_says_it_is_running(tmp_path: Path) -> None:
    supervisor = Supervisor([("node", ["unused"])], None, state_dir=tmp_path, drain_timeout=300.0)
    assert supervisor._drain_wait() == 315.0  # nothing published: its own figure
    publish_drain_timeout(tmp_path, 900.0)  # config raised, or a longer restart --timeout
    assert published_drain_timeout(tmp_path) == 900.0
    assert supervisor._drain_wait() == 915.0
    (tmp_path / "drain-timeout").write_text("not json")
    assert supervisor._drain_wait() == 315.0
