"""Python side of the Node sandbox: spawn `runner.mjs` and speak JSON-RPC to it."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from ahp_host_flow.engine import AgentCall, Dispatcher
from ahp_host_flow.sandbox import protocol

RUNNER: Final = Path(__file__).with_name("runner.mjs")
# Passed as `args` when the caller gives none; the script then sees `undefined`.
NO_ARGS: Final = object()


class SandboxError(Exception):
    """Node died, or broke the protocol."""

    def __init__(self, message: str, *, code: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.stderr = stderr


class ScriptError(SandboxError):
    """The script threw."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        stderr: str = "",
        js_name: str | None = None,
        stack: str | None = None,
    ) -> None:
        super().__init__(message, code=code, stderr=stderr)
        self.js_name = js_name
        self.stack = stack


class MetaError(SandboxError):
    """`meta` is not a pure literal."""


class ScriptSyntaxError(SandboxError):
    """The source did not parse, or used import/export."""


def _error_from(error: Mapping[str, Any], stderr: str) -> SandboxError:
    raw_code = error.get("code")
    code = raw_code if isinstance(raw_code, int) else None
    message = str(error.get("message", ""))
    data = error.get("data")
    if code == protocol.SCRIPT_ERROR:
        info = data if isinstance(data, Mapping) else {}
        name, stack = info.get("name"), info.get("stack")
        return ScriptError(
            message,
            code=code,
            stderr=stderr,
            js_name=name if isinstance(name, str) else None,
            stack=stack if isinstance(stack, str) else None,
        )
    if code == protocol.META_ERROR:
        return MetaError(message, code=code, stderr=stderr)
    if code == protocol.SYNTAX_ERROR:
        return ScriptSyntaxError(message, code=code, stderr=stderr)
    return SandboxError(message, code=code, stderr=stderr)


class Sandbox:
    """One Node subprocess running `runner.mjs`."""

    def __init__(self, node: str, *, runner: Path = RUNNER) -> None:
        self._node = node
        self._runner = runner
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._handlers: set[asyncio.Task[None]] = set()
        self._stderr: collections.deque[str] = collections.deque(maxlen=protocol.STDERR_TAIL_LINES)
        self._pending: dict[int, asyncio.Future[Mapping[str, Any]]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._dead: SandboxError | None = None
        self._run_lock = asyncio.Lock()
        self._dispatcher: Dispatcher | None = None
        self._on_log: Callable[[str], None] | None = None
        self._on_phase: Callable[[str], None] | None = None
        self._agent_tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> Sandbox:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self._node,
            str(self._runner),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=protocol.MAX_LINE_BYTES,
        )
        proc = self._proc
        if proc.stdout is None or proc.stderr is None:
            raise SandboxError("node started without stdout/stderr pipes")
        self._tasks = [
            asyncio.create_task(self._read_stdout(proc, proc.stdout)),
            asyncio.create_task(self._read_stderr(proc.stderr)),
        ]

    async def aclose(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
                await proc.stdin.wait_closed()
        try:
            await asyncio.wait_for(proc.wait(), protocol.SHUTDOWN_TIMEOUT)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        tasks = [*self._tasks, *self._handlers]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = []
        self._handlers.clear()
        self._fail_all(SandboxError(f"sandbox closed ({proc.returncode})"))

    def _fail_all(self, exc: SandboxError) -> None:
        if self._dead is None:
            self._dead = exc
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            self._stderr.append(line.decode("utf-8", errors="replace").rstrip("\r\n"))

    async def _read_stdout(
        self, proc: asyncio.subprocess.Process, stream: asyncio.StreamReader
    ) -> None:
        try:
            while line := await stream.readline():
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    raise ValueError("message is not a JSON object")
                self._dispatch(msg)
        except (ValueError, asyncio.LimitOverrunError) as exc:
            self._fail_all(SandboxError(f"bad message from node: {exc}", stderr=self.stderr_tail))
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return
        await proc.wait()
        # Give the stderr reader a moment to see EOF so the tail is complete.
        await asyncio.sleep(0.05)
        self._fail_all(
            SandboxError(
                f"node exited ({proc.returncode}): {self.stderr_tail}", stderr=self.stderr_tail
            )
        )

    def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method is None:
            request_id = msg.get("id")
            if request_id is None and "error" in msg:
                # Node could not tell which request it failed (a frame it could
                # not parse), so no pending request can be answered any more.
                error = msg["error"] if isinstance(msg["error"], Mapping) else {}
                self._fail_all(
                    SandboxError(
                        f"node rejected a message: {error.get('message', error)}",
                        stderr=self.stderr_tail,
                    )
                )
                return
            future = self._pending.pop(request_id, None) if isinstance(request_id, int) else None
            if future is not None and not future.done():
                future.set_result(msg)
        elif "id" in msg:
            task = asyncio.create_task(self._on_request(msg))
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)
        else:
            self._on_notification(str(method), msg.get("params"))

    async def _on_request(self, msg: Mapping[str, Any]) -> None:
        if msg.get("method") == "agent":
            task = asyncio.current_task()
            if task is not None:
                self._agent_tasks.add(task)
            try:
                await self._on_agent(msg)
            finally:
                if task is not None:
                    self._agent_tasks.discard(task)
            return
        await self._write_error(
            msg["id"], protocol.METHOD_NOT_FOUND, f"method not found: {msg['method']}"
        )

    async def _write_error(self, request_id: Any, code: int, message: str) -> None:
        await self._write(
            {
                "jsonrpc": protocol.JSONRPC,
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    async def _on_agent(self, msg: Mapping[str, Any]) -> None:
        dispatcher = self._dispatcher
        if dispatcher is None:
            await self._write_error(msg["id"], protocol.INTERNAL_ERROR, "no run is active")
            return
        params = msg.get("params") or {}
        ordinal = int(params["ordinal"])
        call = AgentCall(
            ordinal=ordinal,
            prompt=str(params["prompt"]),
            opts=dict(params.get("opts") or {}),
        )
        value: Any = None
        try:
            result = await dispatcher.run_agent(call)
            json.dumps(result.value, allow_nan=False)
            value = result.value
        except Exception as exc:
            # M0's interim rule: agent() returns null on error (plan §3).
            if self._on_log is not None:
                self._on_log(f"agent() #{ordinal} failed: {exc}")
        await self._write(
            {"jsonrpc": protocol.JSONRPC, "id": msg["id"], "result": {"value": value}}
        )

    def _on_notification(self, method: str, params: Any) -> None:
        if not isinstance(params, Mapping):
            return
        if method == "log" and self._on_log is not None:
            self._on_log(str(params.get("message", "")))
        elif method == "phase" and self._on_phase is not None:
            self._on_phase(str(params.get("title", "")))

    async def _write(self, msg: Mapping[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise SandboxError("sandbox is not running", stderr=self.stderr_tail)
        # ASCII only: Node's readline also splits on raw U+2028/U+2029 (protocol.py).
        data = json.dumps(msg, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        async with self._write_lock:
            self._proc.stdin.write(data.encode("utf-8") + b"\n")
            await self._proc.stdin.drain()

    async def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        if self._dead is not None:
            raise self._dead
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {
                    "jsonrpc": protocol.JSONRPC,
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
        except OSError as exc:
            self._pending.pop(request_id, None)
            raise self._dead or SandboxError(
                f"cannot write to node: {exc}", stderr=self.stderr_tail
            ) from exc
        reply = await future
        error = reply.get("error")
        if error is not None:
            raise _error_from(error, self.stderr_tail)
        return reply.get("result")

    async def meta(self, source: str, *, filename: str = "workflow.js") -> dict[str, Any] | None:
        """Read `meta` from a script without running it."""
        result = await self._request("meta", {"source": source, "filename": filename})
        meta = result["meta"]
        assert meta is None or isinstance(meta, dict)
        return meta

    async def run(
        self,
        source: str,
        *,
        dispatcher: Dispatcher,
        args: object = NO_ARGS,
        filename: str = "workflow.js",
        on_log: Callable[[str], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
    ) -> Any:
        """Run a script; `agent()` calls go to `dispatcher`. Returns the script's value."""
        async with self._run_lock:
            self._dispatcher = dispatcher
            self._on_log = on_log
            self._on_phase = on_phase
            try:
                params: dict[str, Any] = {"source": source, "filename": filename}
                if args is not NO_ARGS:
                    params["args"] = args
                result = await self._request("run", params)
                return result["value"]
            finally:
                self._dispatcher = None
                self._on_log = None
                self._on_phase = None
                tasks = list(self._agent_tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


async def extract_meta(
    source: str, *, node: str, filename: str = "workflow.js"
) -> dict[str, Any] | None:
    """Read `meta` from a script with a throwaway sandbox."""
    async with Sandbox(node) as sandbox:
        return await sandbox.meta(source, filename=filename)


async def run_script(
    source: str,
    *,
    node: str,
    dispatcher: Dispatcher,
    args: object = NO_ARGS,
    filename: str = "workflow.js",
    on_log: Callable[[str], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> Any:
    """Run a script with a throwaway sandbox."""
    async with Sandbox(node) as sandbox:
        return await sandbox.run(
            source,
            dispatcher=dispatcher,
            args=args,
            filename=filename,
            on_log=on_log,
            on_phase=on_phase,
        )
