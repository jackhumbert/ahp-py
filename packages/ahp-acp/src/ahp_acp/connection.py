"""ACP's transport: newline-delimited JSON-RPC 2.0 over an agent's stdio.

The agent is a subprocess. The client sends requests (`initialize`,
`session/new`, `session/prompt`) and notifications (`session/cancel`); the agent
sends back `session/update` notifications and requests of its own
(`session/request_permission`).

Two ordering rules matter:

- Notifications are handled one at a time, in arrival order, before the next
  line is read. An agent streams a turn's updates and *then* answers the
  prompt, so every update is handled before the prompt's response resolves.
- Requests from the agent are handled on their own tasks. A permission request
  waits for a human; the updates behind it must keep flowing meanwhile.

A notification handler must therefore never await a request of its own: the
read loop is waiting on the handler, so the answer could never be read.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import itertools
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from ahp_acp.errors import (
    INTERNAL_ERROR,
    AcpError,
    AgentExitedError,
    RequestTimeoutError,
)

log = logging.getLogger(__name__)

#: One line is one message; a replayed session or a large file can be big.
LINE_LIMIT: Final = 64 * 1024 * 1024
#: How long a closing agent gets at each step of shutdown (after stdin closes,
#: after SIGTERM) before the next, harder one.
EXIT_GRACE: Final = 3.0
#: Lines of the agent's stderr kept for the error when it dies.
STDERR_TAIL: Final = 20
_CHUNK: Final = 64 * 1024

NotificationHandler = Callable[[str, Any], Awaitable[None]]
RequestHandler = Callable[[str, Any], Awaitable[Any]]
OversizePolicy = Literal["close", "skip"]


class _OversizeError(Exception):
    """A line passed the limit under the `close` policy."""


def resolve_command(command: Sequence[str]) -> list[str]:
    """`command` with its program found on PATH (Windows: including `.cmd`)."""
    if not command:
        raise ValueError("an empty agent command")
    program = shutil.which(command[0]) or command[0]
    return [program, *command[1:]]


class AcpConnection:
    """One JSON-RPC peer over a pair of streams, usually a spawned agent's stdio.

    Build one with `spawn`. Constructing it directly takes any reader/writer
    pair, which is how tests drive it without a process.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | Any,
        *,
        on_notification: NotificationHandler,
        on_request: RequestHandler,
        process: asyncio.subprocess.Process | None = None,
        process_group: bool = False,
        stream_limit: int = LINE_LIMIT,
        on_oversize: OversizePolicy = "close",
        on_stderr: Callable[[str], None] | None = None,
        grace: float = EXIT_GRACE,
    ) -> None:
        if on_oversize not in ("close", "skip"):
            raise ValueError(f"on_oversize must be 'close' or 'skip', not {on_oversize!r}")
        self._reader = reader
        self._writer = writer
        self._on_notification = on_notification
        self._on_request = on_request
        self._process = process
        self._process_group = process_group
        self._stream_limit = stream_limit
        self._on_oversize: OversizePolicy = on_oversize
        self._on_stderr = on_stderr
        self._grace = grace
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL)
        self._closed = asyncio.Event()
        self._read_task = asyncio.create_task(self._read_loop())
        self._stderr_task: asyncio.Task[None] | None = None
        if process is not None and process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))

    @classmethod
    async def spawn(
        cls,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        inherit_env: bool = True,
        new_process_group: bool = True,
        user: str | int | None = None,
        group: str | int | None = None,
        umask: int | None = None,
        extra_groups: Sequence[str | int] | None = None,
        stream_limit: int = LINE_LIMIT,
        on_oversize: OversizePolicy = "close",
        on_stderr: Callable[[str], None] | None = None,
        grace: float = EXIT_GRACE,
        on_notification: NotificationHandler,
        on_request: RequestHandler,
    ) -> AcpConnection:
        """Start the agent.

        `inherit_env=False` gives the child exactly `env` and nothing from this
        process. `new_process_group` (POSIX) puts the agent in its own session so
        shutdown can signal its whole group, grandchildren included. `user`,
        `group`, `umask` and `extra_groups` are POSIX-only and passed to the
        subprocess call. Setting `user` drops supplementary groups unless
        `extra_groups` names the ones to keep.
        """
        resolved = resolve_command(argv)
        kwargs: dict[str, Any] = {}
        grouped = False
        if sys.platform == "win32":
            # No console window per agent when the client runs from a Scheduled Task.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            if new_process_group:
                kwargs["start_new_session"] = True
                grouped = True
            if user is not None:
                kwargs["user"] = user
                kwargs["extra_groups"] = []
            if extra_groups is not None:
                kwargs["extra_groups"] = list(extra_groups)
            if group is not None:
                kwargs["group"] = group
            if umask is not None:
                kwargs["umask"] = umask
        child_env = {**os.environ, **(env or {})} if inherit_env else dict(env or {})
        process = await asyncio.create_subprocess_exec(
            *resolved,
            cwd=None if cwd is None else str(Path(cwd)),
            env=child_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        assert process.stdout is not None
        assert process.stdin is not None
        log.debug("started ACP agent pid %s: %s", process.pid, resolved)
        return cls(
            process.stdout,
            process.stdin,
            on_notification=on_notification,
            on_request=on_request,
            process=process,
            process_group=grouped,
            stream_limit=stream_limit,
            on_oversize=on_oversize,
            on_stderr=on_stderr,
            grace=grace,
        )

    # -- state -----------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def returncode(self) -> int | None:
        return self._process.returncode if self._process is not None else None

    @property
    def stderr_tail(self) -> str:
        """The last lines the agent wrote to stderr (kept even with `on_stderr`)."""
        return "\n".join(self._stderr)

    # -- outgoing --------------------------------------------------------------

    async def _send(self, message: Mapping[str, Any]) -> None:
        line = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n"
        async with self._write_lock:
            if self.closed:
                raise AgentExitedError(self._exit_message())
            self._writer.write(line.encode("utf-8"))
            try:
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                raise AgentExitedError(self._exit_message()) from exc

    async def request(
        self, method: str, params: Any = None, *, timeout: float | None = None
    ) -> Any:
        """Send a request and wait for its result.

        Raises `AcpError` on an error reply, `AgentExitedError` if the agent
        goes away, `RequestTimeoutError` after `timeout` seconds. A timed-out
        request is abandoned, not cancelled: a late reply is ignored.
        """
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params or {}})
            if timeout is None:
                return await future
            try:
                return await asyncio.wait_for(future, timeout)
            except TimeoutError:
                raise RequestTimeoutError(method, timeout) from None
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"method": method, "params": params or {}})

    # -- incoming --------------------------------------------------------------

    async def _lines(self) -> AsyncIterator[bytes]:
        """Complete lines from the reader, none longer than the limit.

        Reads in chunks rather than with `readline`, so an over-long line under
        the `skip` policy is discarded up to its newline and reading carries on
        in step with the stream. Under `close`, `_OversizeError` is raised.
        """
        buffer = bytearray()
        discarding = False
        while True:
            chunk = await self._reader.read(_CHUNK)
            if not chunk:
                if buffer and not discarding:
                    yield bytes(buffer)
                return
            start = 0
            while start <= len(chunk):
                end = chunk.find(b"\n", start)
                piece = chunk[start:] if end < 0 else chunk[start:end]
                start = len(chunk) + 1 if end < 0 else end + 1
                if not discarding:
                    buffer += piece
                    if len(buffer) > self._stream_limit:
                        if self._on_oversize == "close":
                            raise _OversizeError
                        log.warning(
                            "ACP agent sent a line over %d bytes; skipping it", self._stream_limit
                        )
                        buffer.clear()
                        discarding = True
                if end < 0:
                    break
                if discarding:
                    discarding = False
                elif buffer or piece:
                    yield bytes(buffer)
                    buffer.clear()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._lines():
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    # Banners and stray logging on stdout: not ours to act on.
                    log.debug("ACP agent wrote a non-JSON line: %.200s", text)
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message)
        except _OversizeError:
            log.error("ACP agent sent a line over %d bytes; closing", self._stream_limit)
        except Exception:
            log.exception("reading from the ACP agent failed")
        finally:
            await self._settle_exit()
            self._closed.set()
            error = AgentExitedError(self._exit_message())
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            params = message.get("params")
            if "id" in message:
                task = asyncio.create_task(self._answer(message["id"], method, params))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                try:
                    await self._on_notification(method, params)
                except Exception:
                    log.exception("handling ACP notification %s failed", method)
            return
        request_id = message.get("id")
        future = self._pending.get(request_id) if isinstance(request_id, int) else None
        if future is None or future.done():
            return  # a response nobody waits for any more (a cancelled turn's prompt)
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            code = error.get("code", INTERNAL_ERROR)
            future.set_exception(
                AcpError(
                    str(error.get("message", "error")),
                    code if isinstance(code, int) else INTERNAL_ERROR,
                    error.get("data"),
                )
            )
        else:
            future.set_result(message.get("result"))

    async def _answer(self, request_id: Any, method: str, params: Any) -> None:
        try:
            result = await self._on_request(method, params)
        except AcpError as exc:
            reply: dict[str, Any] = {
                "id": request_id,
                "error": {"code": exc.code, "message": exc.message},
            }
            if exc.data is not None:
                reply["error"]["data"] = exc.data
        except Exception as exc:
            log.exception("handling ACP request %s failed", method)
            reply = {"id": request_id, "error": {"code": INTERNAL_ERROR, "message": str(exc)}}
        else:
            reply = {"id": request_id, "result": result}
        with contextlib.suppress(AcpError):
            await self._send(reply)

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        with contextlib.suppress(Exception):
            while line := await stream.readline():
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                self._stderr.append(text)
                if self._on_stderr is not None:
                    try:
                        self._on_stderr(text)
                    except Exception:
                        log.exception("the on_stderr callback failed")
                else:
                    log.debug("agent stderr: %s", text)

    async def _settle_exit(self) -> None:
        """After stdout closes, give the process a moment to report its exit
        code and last stderr lines, which are what explain a crash."""
        waits: list[Awaitable[Any]] = []
        if self._process is not None:
            waits.append(self._process.wait())
        if self._stderr_task is not None:
            waits.append(asyncio.shield(self._stderr_task))
        for wait in waits:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(wait, 1.0)

    def _exit_message(self) -> str:
        code = self.returncode
        message = "the ACP agent exited" + (f" with code {code}" if code is not None else "")
        tail = self.stderr_tail
        return f"{message}\n{tail}" if tail else message

    # -- lifecycle -------------------------------------------------------------

    async def aclose(self) -> None:
        """Close stdin (ACP agents exit on EOF), then terminate, then kill.

        Each step waits `grace` seconds first. With a process group the signals
        go to the whole group, so grandchildren do not outlive the agent.
        """
        with contextlib.suppress(Exception):
            self._writer.close()
        process = self._process
        if process is not None:
            await self._stop(process)
        for task in (self._read_task, self._stderr_task, *self._tasks):
            if task is not None and not task.done():
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._read_task

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), self._grace)
        if sys.platform == "win32":
            if process.returncode is None:
                await _taskkill(process)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), self._grace)
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not self._alive(process):
                return
            self._signal(process, sig)
            deadline = asyncio.get_running_loop().time() + self._grace
            while self._alive(process) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), self._grace)

    def _alive(self, process: asyncio.subprocess.Process) -> bool:
        """The agent, or with a process group anything left in it, still runs."""
        if process.returncode is None:
            return True
        if not self._process_group:
            return False
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def _signal(self, process: asyncio.subprocess.Process, sig: int) -> None:
        try:
            if self._process_group:
                os.killpg(process.pid, sig)
            else:
                process.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass


async def _taskkill(process: asyncio.subprocess.Process) -> None:
    """Kill the agent and its children on Windows.

    A `.cmd` shim (npm's) is `cmd.exe` running `node`: killing only the shim
    would leave the agent running. `taskkill /T` takes the whole tree.
    """
    killer = await asyncio.create_subprocess_exec(
        "taskkill",
        "/T",
        "/F",
        "/PID",
        str(process.pid),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    await killer.wait()
