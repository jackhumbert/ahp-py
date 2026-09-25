"""Keep a node running: `supervise`, and `install` / `uninstall` it at login.

    agent-host-node install --config ~/.config/agent-host/node.toml
    agent-host-node uninstall

`supervise` runs the node (`run`) as a child process and starts it again when it
exits, backing off while it keeps failing. It does the same for the config's
`tunnel` command, if any (an `ssh -N -R ...` to a broker). Each exit is written
to `<log_file>.supervisor.log` (or stderr), with the child's own stderr, so a
crash that happens before logging is set up still leaves a trace.

`install` makes the supervisor start at login, as the current user -- the
agents need the user's own sign-ins and config, which a Windows service running
as another account would not have:

* Windows: a Scheduled Task with a logon trigger running
  ``pythonw.exe -m agent_host_server.node supervise ...`` directly -- no console
  window and no script in between. Task Scheduler's own restart-on-failure is
  set as well, though it does not cover a run started on demand, which is why
  the supervisor does the restarting.
* macOS: a launchd agent (``~/Library/LaunchAgents/<label>.plist``) with
  ``KeepAlive``.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import list2cmdline
from typing import IO
from xml.sax.saxutils import escape

DEFAULT_TASK_NAME = "agent-host-node"
DEFAULT_LAUNCHD_LABEL = "io.agent-host.node"

#: Windows: start a console program (ssh.exe) with no console window.
_CREATE_NO_WINDOW = 0x08000000

#: A child that ran this long before exiting was healthy; its backoff resets.
_HEALTHY_SECONDS = 60.0
_MAX_BACKOFF = 60.0


def python_for_service() -> str:
    """The interpreter a service should run: `pythonw.exe` on Windows (no console)."""
    exe = Path(sys.executable)
    if os.name == "nt":
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(exe)


def node_command(
    config: Path, log_file: Path | None, *, python: str | None = None, verb: str = "run"
) -> list[str]:
    command = [
        python or sys.executable,
        "-m",
        "agent_host_server.node",
        verb,
        "--config",
        str(config),
    ]
    if log_file is not None:
        command += ["--log-file", str(log_file)]
    return command


# ─── supervise ──────────────────────────────────────────────────────────


@dataclass
class _Child:
    name: str
    command: list[str]
    process: subprocess.Popen[bytes] | None = None
    started: float = 0.0
    failures: int = 0
    next_start: float = 0.0
    history: list[int] = field(default_factory=list)

    def backoff(self) -> float:
        return float(min(_MAX_BACKOFF, 2 ** max(0, self.failures - 1)))


class Supervisor:
    """Start each command, and start it again whenever it exits."""

    def __init__(self, children: Sequence[tuple[str, list[str]]], log: Path | None) -> None:
        self.children = [_Child(name, list(command)) for name, command in children]
        self.log_path = log
        self._stopping = False

    def _log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} supervisor: {message}\n"
        if self.log_path is None:
            if sys.stderr is not None:
                sys.stderr.write(line)
                sys.stderr.flush()
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as out:
            out.write(line)

    def _output(self) -> IO[bytes] | int | None:
        """Where a child's output goes: the supervisor log, else ours."""
        if self.log_path is None:
            # Under pythonw there is no stderr to inherit.
            return subprocess.DEVNULL if sys.stderr is None else None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        return self.log_path.open("ab")

    def _start(self, child: _Child) -> None:
        flags = _CREATE_NO_WINDOW if os.name == "nt" else 0
        output = self._output()
        try:
            child.process = subprocess.Popen(
                child.command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        except OSError as exc:
            child.process = None
            child.failures += 1
            child.next_start = time.monotonic() + child.backoff()
            self._log(f"{child.name}: could not start ({exc}); retrying in {child.backoff():.0f}s")
            return
        finally:
            if not isinstance(output, int) and output is not None:
                output.close()  # the child holds its own handle
        child.started = time.monotonic()
        self._log(f"{child.name}: started (pid {child.process.pid})")

    def poll_once(self, now: float | None = None) -> None:
        """Start whatever is due; notice whatever has exited."""
        now = time.monotonic() if now is None else now
        for child in self.children:
            if child.process is None:
                if now >= child.next_start:
                    self._start(child)
                continue
            code = child.process.poll()
            if code is None:
                continue
            ran = now - child.started
            child.history.append(code)
            child.failures = 1 if ran >= _HEALTHY_SECONDS else child.failures + 1
            child.process = None
            child.next_start = now + child.backoff()
            self._log(
                f"{child.name}: exited with {code} after {ran:.0f}s; "
                f"restarting in {child.backoff():.0f}s"
            )

    def stop(self, *_: object) -> None:
        self._stopping = True

    def run(self, interval: float = 1.0) -> int:
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Not the main thread, or not a signal this OS delivers.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, self.stop)
        self._log("supervising " + ", ".join(child.name for child in self.children))
        try:
            while not self._stopping:
                self.poll_once()
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        for child in self.children:
            if child.process is not None and child.process.poll() is None:
                child.process.terminate()
                try:
                    child.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.process.kill()
        self._log("stopped")
        return 0


def supervisor_log(log_file: Path | None) -> Path | None:
    return log_file.with_name(log_file.name + ".supervisor.log") if log_file else None


# ─── install: Windows ───────────────────────────────────────────────────


def windows_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def task_xml(command: Sequence[str], *, user: str, working_directory: str, description: str) -> str:
    """A Task Scheduler definition: at `user`'s logon, run `command`, keep it up."""
    program, *arguments = command
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(program)}</Command>
      <Arguments>{escape(list2cmdline(arguments))}</Arguments>
      <WorkingDirectory>{escape(working_directory)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, check=check)


def install_windows(command: Sequence[str], name: str, state_dir: Path) -> str:
    xml = task_xml(
        command,
        user=windows_user(),
        working_directory=str(Path.home()),
        description="agent-host-node: this machine's agents, served over AHP.",
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    definition = state_dir / f"{name}.task.xml"
    definition.write_text(xml, encoding="utf-16")
    # Stop a running copy first: a new definition does not replace a live process.
    _schtasks("/End", "/TN", name, check=False)
    _schtasks("/Create", "/TN", name, "/XML", str(definition), "/F")
    _schtasks("/Run", "/TN", name)
    return f"Scheduled Task {name!r} registered (runs at logon) and started"


def uninstall_windows(name: str) -> str:
    _schtasks("/End", "/TN", name, check=False)
    result = _schtasks("/Delete", "/TN", name, "/F", check=False)
    if result.returncode != 0:
        return f"no Scheduled Task {name!r} to remove"
    return f"Scheduled Task {name!r} removed"


# ─── install: macOS ─────────────────────────────────────────────────────


def launchd_plist(command: Sequence[str], *, label: str, log: Path | None) -> bytes:
    spec: dict[str, object] = {
        "Label": label,
        "ProgramArguments": list(command),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "WorkingDirectory": str(Path.home()),
    }
    if log is not None:
        spec["StandardOutPath"] = str(log)
        spec["StandardErrorPath"] = str(log)
    return plistlib.dumps(spec)


def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def install_macos(command: Sequence[str], label: str, log: Path | None) -> str:
    path = _plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, check=False)
    path.write_bytes(launchd_plist(command, label=label, log=log))
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    return f"launchd agent {label!r} installed at {path} and started"


def uninstall_macos(label: str) -> str:
    path = _plist_path(label)
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True, check=False
    )
    if not path.exists():
        return f"no launchd agent {label!r} to remove"
    path.unlink()
    return f"launchd agent {label!r} removed"


__all__ = [
    "DEFAULT_LAUNCHD_LABEL",
    "DEFAULT_TASK_NAME",
    "Supervisor",
    "install_macos",
    "install_windows",
    "launchd_plist",
    "node_command",
    "python_for_service",
    "supervisor_log",
    "task_xml",
    "uninstall_macos",
    "uninstall_windows",
]
