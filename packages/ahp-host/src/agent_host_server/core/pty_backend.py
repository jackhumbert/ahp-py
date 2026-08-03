"""A real POSIX pty backend. **This runs commands.**

Installing this is the single largest privilege escalation available to this
library: every other surface reads, writes or describes, and this one executes.
AHP defines no authentication, so a host that installs a pty backend is handing
a shell to whoever completes ``initialize``. That is why it is not the default,
is not reachable by upgrading, and has to be constructed by name -- the same
posture as :class:`~agent_host_server.core.resources.RootedFilesystemResourceProvider`,
one notch stricter.

What it does NOT do, deliberately:

* **It does not inherit the environment.** ``env=None`` on a
  :class:`~agent_host_server.core.terminals.TerminalRequest` means "supply
  nothing", and the default here is a small explicit set -- ``TERM``, ``PATH``,
  ``HOME``, ``SHELL``, ``LANG``. A host process typically holds tokens,
  registry credentials and cloud config in its environment, and a shell a peer
  asked for must not start life holding them.
* **It does not strip escape sequences.** Output reaches the sink exactly as
  the child wrote it. Shell integration is parsed downstream by
  :class:`~agent_host_server.core.terminals.ShellIntegrationParser`, which is
  the only place that can survive a sequence split across two reads.
* **It does not choose a working directory.** ``cwd`` comes from the request,
  and a backend that defaulted to the host's own directory would silently
  expose it.

The child is started in its own **session** (``setsid``), which makes the pty
its controlling terminal -- without that, job control and Ctrl-C do not work
and the shell will say so. It also means the child is a process-group leader,
so :meth:`PtyTerminalProcess.kill` signals the whole group: killing only the
leader leaves its children running, attached to a pty nobody is reading, which
is how a "closed" terminal keeps burning CPU.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import shutil
import signal
import struct
import termios
from collections.abc import Mapping, Sequence
from typing import Final

from agent_host_server.core.resources import path_from_file_uri
from agent_host_server.core.terminals import (
    OutputSink,
    TerminalProcess,
    TerminalRequest,
    terminal_refused,
)

__all__ = ["PtyTerminalBackend", "PtyTerminalProcess"]

_log = logging.getLogger(__name__)

#: How much to lift off the pty per read. A pty's kernel buffer is small; this
#: is comfortably larger, so a burst of output is a few reads rather than many.
_READ_SIZE = 65536

#: How many reads `_detach` makes while rescuing the tail. Bounded because it
#: runs on the event-loop thread: the parent holds the slave, so the master
#: never reports EOF on its own and an unbounded loop against a chatty survivor
#: process would wedge the loop. 64 x _READ_SIZE is far more than any command's
#: trailing burst.
_DRAIN_ROUNDS: Final = 64

#: Seconds between the hangup and SIGKILL. Short, because SIGHUP is the signal
#: a shell is built to obey -- this is a backstop for a child that ignores it,
#: not the normal path.
_GRACE_SECONDS = 1.0

#: The default environment. Small and explicit: everything here is needed for a
#: shell to behave like a terminal, and nothing here carries a credential.
_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "USER", "SHELL")


def _as_local_path(value: str) -> str:
    """A working directory as a filesystem path, from a URI or a path.

    `CreateTerminalParams.cwd` is a **URI** -- so is `TerminalState.cwd`, and so
    is the `cwd` on `terminal/cwdChanged`. The terminal channel is URIs
    throughout. Passing one to `os.path.isdir` fails, and the failure is
    user-facing: VS Code renders the refusal verbatim as "The terminal process
    failed to launch: ... file:///Users/... is not a directory."

    A bare path is accepted too. An embedder constructing a
    :class:`~agent_host_server.core.terminals.TerminalRequest` by hand will
    write a path, and refusing it would be pedantry about a string this backend
    can read either way.
    """
    if not value.startswith("file:"):
        # No scheme, or a scheme this backend does not mediate. A Windows drive
        # letter (`C:\...`) also lands here, correctly.
        return value
    return str(path_from_file_uri(value))


def _default_environment() -> dict[str, str]:
    inherited = {k: v for k in _SAFE_ENV_KEYS if (v := os.environ.get(k)) is not None}
    # `dumb` would disable colour and most cursor addressing; `xterm-256color`
    # is what a client rendering VT sequences expects, and the client tells us
    # it can parse them via `TerminalState.isPty`.
    inherited.setdefault("TERM", "xterm-256color")
    return inherited


def _default_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell and os.path.isabs(shell) and os.access(shell, os.X_OK):
        return shell
    for candidate in ("/bin/zsh", "/bin/bash", "/bin/sh"):
        if os.access(candidate, os.X_OK):
            return candidate
    raise terminal_refused("no usable shell was found on this host")


class PtyTerminalProcess:
    """One live child on one pty. Implements
    :class:`~agent_host_server.core.terminals.TerminalProcess`."""

    is_pty = True

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        master_fd: int,
        output: OutputSink,
        slave_fd: int | None = None,
    ) -> None:
        self._process = process
        self._master = master_fd
        #: The PARENT's copy of the slave, held open so the kernel does not
        #: discard the pty buffer when the child exits. Closed with the master.
        self._slave = slave_fd
        self._output = output
        self._closed = False
        self._loop = asyncio.get_running_loop()
        self._exit: int | None = None
        # Registered with the loop rather than read in a task: a pty master is
        # a plain fd, and `add_reader` gives us the read without a thread and
        # without a poll interval.
        self._loop.add_reader(self._master, self._drain)
        self._reaper = asyncio.create_task(self._reap())

    # ─── the protocol ────────────────────────────────────────────────────

    async def write(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            os.write(self._master, data)
        except OSError as error:
            # The child is gone. Not an error the caller can act on -- the exit
            # is already on its way through `_reap` -- so it is logged, not
            # raised, and the terminal closes by the normal path.
            _log.debug("write to a closed pty: %s", error)

    async def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        with contextlib.suppress(OSError):
            # TIOCSWINSZ takes rows FIRST. Transposing them is silent -- the
            # shell simply wraps at the wrong column -- so it is worth being
            # explicit that this order is not the (cols, rows) of the caller.
            fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def kill(self) -> None:
        if self._process.returncode is not None:
            return
        # SIGHUP, not SIGTERM. An interactive shell IGNORES SIGTERM -- zsh and
        # bash both do -- so the old code sent a signal nothing acted on and
        # then waited the full grace period before SIGKILL. Measured at 3.00s
        # per terminal, every time, which is a three-second stall on closing a
        # terminal tab. A hangup is what a terminal going away actually means,
        # and a shell exits on it immediately.
        #
        # The GROUP, not the process: `start_new_session` made the child a
        # group leader, so its own children are in that group, and signalling
        # only the leader leaves them running on a pty nobody reads.
        self._signal_group(signal.SIGHUP)
        try:
            await asyncio.wait_for(self._process.wait(), timeout=_GRACE_SECONDS)
            return
        except TimeoutError:
            pass
        self._signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(self._process.wait(), timeout=_GRACE_SECONDS)
        except TimeoutError:
            self._signal_group(signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=_GRACE_SECONDS)

    async def wait(self) -> int | None:
        await self._process.wait()
        return self._exit if self._exit is not None else self._process.returncode

    # ─── internals ───────────────────────────────────────────────────────

    def _signal_group(self, sig: signal.Signals) -> None:
        if self._process.pid is None:
            return
        try:
            os.killpg(os.getpgid(self._process.pid), sig)
        except (ProcessLookupError, PermissionError):
            # Already reaped, or not ours. Either way there is nothing to kill.
            with contextlib.suppress(ProcessLookupError):
                self._process.send_signal(sig)

    def _drain(self) -> None:
        """Called by the loop whenever the pty has bytes."""
        try:
            chunk = os.read(self._master, _READ_SIZE)
        except BlockingIOError:
            return
        except OSError:
            # EIO on a pty master means the last slave closed -- the normal way
            # a pty reports EOF, not a failure.
            self._detach()
            return
        if not chunk:
            self._detach()
            return
        try:
            self._output(chunk)
        except Exception:
            # An exception here is the HOST's bug, and letting it escape a
            # reader callback tears down the loop's handling of this fd and
            # wedges the terminal in silence.
            _log.exception("terminal output sink raised")

    def _detach(self) -> None:
        """Stop reading. Idempotent: EOF and exit can both reach this."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._loop.remove_reader(self._master)
        # Drain what the reader has not lifted yet. Reachable now precisely
        # because the parent still holds the slave: the buffer survives the
        # child, so there is something here to rescue.
        for _ in range(_DRAIN_ROUNDS):
            try:
                chunk = os.read(self._master, _READ_SIZE)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            try:
                self._output(chunk)
            except Exception:
                _log.exception("terminal output sink raised")
                break
        for fd in (self._master, self._slave):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

    async def _reap(self) -> None:
        """Wait for the child, then make sure the fd goes with it.

        Without the detach the master fd outlives the process, and a host that
        opens terminals over a long life runs out of descriptors -- a failure
        that shows up as an unrelated command failing to start, hours later.

        But the detach may not be IMMEDIATE, and that was a real bug. The child
        exiting makes two things ready at once: this `wait()`, and the reader
        callback holding the output. Detaching as soon as `wait()` returned
        raced the reader and usually won under load -- and losing that race is
        not a delay, it is data loss: probed on macOS, once the last slave
        closes the master reports EOF and the buffered bytes are simply gone
        (`os.read` returns `b""`, not the pending output). Draining inside
        `_detach` therefore cannot work; by then there is nothing left to drain.
        Measured before this: 0 of 8 concurrent commands delivered any output at
        all, and 2 in 300 serially. The symptom is `!ls` rendering an empty
        terminal card while reporting `success: true`.

        So the READER owns the fd's lifetime. `_drain` already detaches when it
        sees EOF, which is the ordered path -- everything lifted, then closed.
        This only forces the issue if EOF never comes, which happens when the
        child forked a survivor that still holds the slave.
        """
        try:
            self._exit = await self._process.wait()
        finally:
            self._detach()


class PtyTerminalBackend:
    """Runs a shell on a pty. Implements
    :class:`~agent_host_server.core.terminals.TerminalBackend`.

    :param shell: what to run when the request names no command. Defaults to
        ``$SHELL``, then the first of zsh/bash/sh that exists.
    :param env: the child's whole environment. ``None`` uses a small explicit
        default; pass ``{}`` for an empty one. The host's own environment is
        never inherited wholesale.
    :param default_cwd: used when the request carries no ``cwd``. ``None``
        means the child inherits the host's directory, which is why it is not
        the default -- an embedder should say where.
    """

    def __init__(
        self,
        *,
        shell: str | None = None,
        env: Mapping[str, str] | None = None,
        default_cwd: str | None = None,
    ) -> None:
        self._shell = shell
        self._env = dict(env) if env is not None else None
        self._default_cwd = default_cwd

    async def create(self, request: TerminalRequest, output: OutputSink) -> TerminalProcess:
        argv: Sequence[str] = request.command or (self._shell or _default_shell(),)
        if not argv:
            raise terminal_refused("no command to run")
        program = shutil.which(argv[0]) or argv[0]
        if not os.access(program, os.X_OK):
            # Refused with a reason rather than handing back a process that is
            # already dead: the client renders this string to the user.
            raise terminal_refused(f"{argv[0]} is not executable")

        requested_cwd = request.cwd or self._default_cwd
        cwd = _as_local_path(requested_cwd) if requested_cwd is not None else None
        if cwd is not None and not os.path.isdir(cwd):
            # The ORIGINAL string in the message, not the converted one: the
            # user recognises what they picked, not what we turned it into.
            raise terminal_refused(f"{requested_cwd} is not a directory")

        environment = dict(request.env) if request.env is not None else None
        if environment is None:
            environment = dict(self._env) if self._env is not None else _default_environment()

        master, slave = os.openpty()
        # Sized BEFORE the child starts, so its first prompt is drawn at the
        # right width. A shell that starts at 80x24 and is resized afterwards
        # redraws, and the redraw is visible.
        with contextlib.suppress(OSError):
            fcntl.ioctl(
                master,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", request.rows or 24, request.cols or 80, 0, 0),
            )
        os.set_blocking(master, False)

        try:
            process = await asyncio.create_subprocess_exec(
                program,
                *argv[1:],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=environment,
                # Makes the pty the CONTROLLING terminal. Without it there is
                # no job control, Ctrl-C reaches nothing, and interactive
                # shells print a warning and behave oddly.
                start_new_session=True,
            )
        except OSError as error:
            os.close(master)
            os.close(slave)
            raise terminal_refused(f"could not start {argv[0]}: {error}") from error
        _log.info("terminal %s: started %s (pid %s)", request.channel, program, process.pid)
        # The parent's slave copy is HELD, not closed here, and handed to the
        # process object to close at the end. It used to be closed as soon as
        # the child had its own, so that the master would see EOF -- but that
        # is precisely what makes the output disappear: with no slave left, a
        # macOS pty master reports EOF and DISCARDS whatever the child wrote
        # last (probed directly -- `os.read` returns `b""`, not the pending
        # bytes). Measured: 0 of 8 concurrent commands delivered any output,
        # and 2 in 300 serially, rendering `!ls` as an empty terminal card
        # while the tool call reported success.
        #
        # Holding it means EOF never arrives on its own, so `_reap` drives
        # closure off `process.wait()` instead -- which it was already awaiting.
        return PtyTerminalProcess(process, master, output, slave)
