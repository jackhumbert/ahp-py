"""ACP's transport: newline-delimited JSON-RPC 2.0 over an agent's stdio.

The agent is a subprocess. The host (the ACP *client*) sends requests
(`initialize`, `session/new`, `session/prompt`) and notifications
(`session/cancel`); the agent sends back `session/update` notifications and
requests of its own (`session/request_permission`).

Two ordering rules matter:

- Notifications are handled one at a time, in arrival order, before the next
  line is read. An agent streams a turn's updates and *then* answers the
  prompt, so every update is published before the prompt's response resolves.
- Requests from the agent are handled on their own tasks. A permission request
  waits for a human; the updates behind it must keep flowing meanwhile.
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
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

log = logging.getLogger(__name__)

#: One line is one message; a replayed session or a large file can be big.
LINE_LIMIT: Final = 64 * 1024 * 1024
#: How long a closing agent gets after its stdin closes, before it is killed.
EXIT_GRACE: Final = 3.0
#: Lines of the agent's stderr kept for the error when it dies.
STDERR_TAIL: Final = 20

METHOD_NOT_FOUND: Final = -32601
INTERNAL_ERROR: Final = -32603

NotificationHandler = Callable[[str, Any], Awaitable[None]]
RequestHandler = Callable[[str, Any], Awaitable[Any]]


class AcpError(Exception):
    """An error response from the agent, or the agent going away."""

    def __init__(self, message: str, code: int = INTERNAL_ERROR, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class AgentExitedError(AcpError):
    """The agent's process ended (or its stdout closed) with requests in flight."""


class MethodNotFoundError(AcpError):
    """Raise from a request handler for a method this client does not implement."""

    def __init__(self, method: str) -> None:
        super().__init__(f"Method not found: {method}", METHOD_NOT_FOUND)


def resolve_command(command: Sequence[str]) -> list[str]:
    """`command` with its program found on PATH (Windows: including `.cmd`)."""
    if not command:
        raise ValueError("an empty agent command")
    program = shutil.which(command[0]) or command[0]
    return [program, *command[1:]]


class AcpConnection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | Any,
        *,
        on_notification: NotificationHandler,
        on_request: RequestHandler,
        process: asyncio.subprocess.Process | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_notification = on_notification
        self._on_request = on_request
        self._process = process
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
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        on_notification: NotificationHandler,
        on_request: RequestHandler,
    ) -> AcpConnection:
        argv = resolve_command(command)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # No console window per session when the host runs from a Scheduled Task.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env={**os.environ, **(env or {})},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT,
            **kwargs,
        )
        assert process.stdout is not None
        assert process.stdin is not None
        log.debug("started ACP agent pid %s: %s", process.pid, argv)
        return cls(
            process.stdout,
            process.stdin,
            on_notification=on_notification,
            on_request=on_request,
            process=process,
        )

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def stderr_tail(self) -> str:
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

    async def request(self, method: str, params: Any = None) -> Any:
        """Send a request and wait for its result; raises `AcpError` on an error reply."""
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params or {}})
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"method": method, "params": params or {}})

    # -- incoming --------------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    line = await self._reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    log.error("ACP agent sent a line over %d bytes; closing", LINE_LIMIT)
                    break
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
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
            future.set_exception(
                AcpError(
                    str(error.get("message", "error")),
                    int(error.get("code", INTERNAL_ERROR)),
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
                "error": {"code": exc.code, "message": str(exc)},
            }
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
                if text:
                    self._stderr.append(text)
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
        code = self._process.returncode if self._process is not None else None
        message = "the ACP agent exited" + (f" with code {code}" if code is not None else "")
        tail = self.stderr_tail()
        return f"{message}\n{tail}" if tail else message

    # -- lifecycle -------------------------------------------------------------

    async def aclose(self) -> None:
        """Close stdin (ACP agents exit on EOF), then kill whatever is left."""
        with contextlib.suppress(Exception):
            self._writer.close()
        process = self._process
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), EXIT_GRACE)
            except TimeoutError:
                await _kill_tree(process)
        for task in (self._read_task, self._stderr_task, *self._tasks):
            if task is not None and not task.done():
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._read_task


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Kill the agent and its children.

    On Windows a `.cmd` shim (npm's `openclaw.cmd`) is `cmd.exe` running `node`:
    killing only the shim would leave the agent running. `taskkill /T` takes the
    whole tree.
    """
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/T",
            "/F",
            "/PID",
            str(process.pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), EXIT_GRACE)
