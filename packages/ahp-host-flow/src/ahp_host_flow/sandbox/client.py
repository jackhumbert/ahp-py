"""Python side of the Node sandbox: spawn `runner.mjs` and speak JSON-RPC to it."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal

from ahp_host_flow.engine import (
    MAX_ITEMS_PER_CALL,
    AbortSignal,
    AgentCall,
    AgentLimitError,
    AgentRecord,
    Dispatcher,
    GateCall,
    GateRecord,
    GateRunner,
    Gates,
    GateUnavailableError,
    JournalError,
    JournalRun,
    Registry,
    ResolvedScript,
    RunOptions,
    Scheduler,
    SchedulerRefusal,
    VerbCall,
    VerbFailedError,
    VerbHost,
    VerbRecord,
    VerbRunner,
    WorkflowRefError,
    load_journal,
    resolve_workflow,
)
from ahp_host_flow.sandbox import protocol
from ahp_host_flow.sandbox.node import MIN_NODE_MAJOR, NodeNotFoundError

RUNNER: Final = Path(__file__).with_name("runner.mjs")
# Passed as `args` when the caller gives none; the script then sees `undefined`.
NO_ARGS: Final = object()
# Seconds a run waits at its end for canvas() calls still being published.
CANVAS_FLUSH_TIMEOUT: Final = 5.0

Language = Literal["js", "py"]

# What `_dispatch` takes at the host when a request line arrives, passed on to its handler.
_Issued = (
    tuple[AgentCall, AgentRecord | None]
    | tuple[GateCall, GateRecord | None]
    | tuple[VerbCall, VerbRecord | None]
    | Exception
    | None
)


def language_of(filename: str) -> Language:
    """The runner a script needs, by file type: `.py` is Python, anything else JavaScript."""
    return "py" if filename.lower().endswith(".py") else "js"


def runner_command(language: Language, node: str | None, *, runner: Path = RUNNER) -> list[str]:
    """The argv that starts a runner for `language`."""
    if language == "py":
        return [sys.executable, "-I", "-m", protocol.PYRUNNER_MODULE]
    if node is None:
        raise NodeNotFoundError()
    return [node, str(runner)]


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


class RunTimeoutError(SandboxError):
    """The run exceeded `RunOptions.timeout`; the Node process was killed."""


class WorkflowRunError(SandboxError):
    """A child workflow failed; the message is what `workflow()` throws in the parent."""


class MetaError(SandboxError):
    """`meta` is not a pure literal."""


class ScriptSyntaxError(SandboxError):
    """The source did not parse, or used import/export."""


def _masked(exc: SandboxError, mask: Callable[[Any], Any] | None) -> SandboxError:
    """*exc* with its message, stderr and stack masked (plan §8 M17 item 4), in place.

    A script's error can quote what it was given -- its args, a line of its source -- and the
    message reaches a log, a terminal or a failed turn, so it is masked like a log line.
    """
    if mask is None:
        return exc
    exc.args = tuple(str(mask(a)) if isinstance(a, str) else a for a in exc.args)
    exc.stderr = str(mask(exc.stderr))
    if isinstance(exc, ScriptError) and exc.stack is not None:
        exc.stack = str(mask(exc.stack))
    return exc


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


@dataclass
class RunState:
    """What every runner of one run shares: the scheduler, options and journal."""

    scheduler: Scheduler
    options: RunOptions
    registry: Registry
    journal: JournalRun | None
    node: str | None
    on_log: Callable[[str], None] | None
    on_phase: Callable[[str], None] | None
    gates: GateRunner
    verbs: VerbRunner
    # Child runners of the other language, alive while the run is.
    children: set[Sandbox] = field(default_factory=set)

    def kill_children(self, exc: SandboxError) -> None:
        for child in list(self.children):
            child._kill(exc)


async def resolve_for_run(state: RunState, ref: object) -> ResolvedScript:
    """Resolve a `workflow()` reference: the journal snapshot first, else the registry."""
    resolved = state.journal.snapshot_for(ref) if state.journal else None
    if resolved is None:
        # WorkflowRefError propagates.
        resolved = await asyncio.to_thread(
            resolve_workflow,
            ref,
            registry=state.registry,
            run_root=state.options.run_root,
            python_dirs=state.options.python_dirs,
        )
    if state.journal:
        state.journal.record_workflow(ref, resolved)
    return resolved


async def run_workflow_child(
    state: RunState, resolved: ResolvedScript, args: object = NO_ARGS
) -> Any:
    """Run a child of the other language in a runner of its own, on the run's scheduler."""
    lang = language_of(resolved.filename)
    if lang == "js" and state.node is None:
        raise WorkflowRunError(
            f"workflow {resolved.filename}: no Node >= {MIN_NODE_MAJOR} found to run it"
        )
    child = Sandbox(state.node, language=lang)
    state.children.add(child)
    try:
        await child.start()
        return await child._run_child(state, resolved, args)
    except ScriptError as exc:
        # The child threw: the same message an in-process child's throw gives.
        raise WorkflowRunError(str(exc)) from None
    except WorkflowRunError:
        raise
    except SandboxError as exc:
        # Syntax, meta, crash or killed.
        raise WorkflowRunError(f"workflow {resolved.filename}: {exc}") from None
    finally:
        state.children.discard(child)
        await child.aclose()


class Sandbox:
    """One runner subprocess: Node running `runner.mjs`, or the Python runner."""

    def __init__(
        self, node: str | None = None, *, runner: Path = RUNNER, language: Language = "js"
    ) -> None:
        self._node = node
        self._runner = runner
        self._language: Language = language
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._handlers: set[asyncio.Task[None]] = set()
        self._stderr: collections.deque[str] = collections.deque(maxlen=protocol.STDERR_TAIL_LINES)
        self._pending: dict[int, asyncio.Future[Mapping[str, Any]]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._dead: SandboxError | None = None
        self._run_lock = asyncio.Lock()
        self._run: RunState | None = None
        # A child connection serves a `workflow()` child of the run; its own `workflow()` fails.
        self._child_connection = False
        self._agent_tasks: set[asyncio.Task[None]] = set()
        self._canvas_tasks: set[asyncio.Task[None]] = set()

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
    def language(self) -> Language:
        return self._language

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *runner_command(self._language, self._node, runner=self._runner),
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

    def _kill(self, exc: SandboxError) -> None:
        """Fail every pending and later request with `exc`, then kill node."""
        self._fail_all(exc)
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()

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
        except Exception as exc:
            self._fail_all(SandboxError(f"sandbox reader failed: {exc!r}", stderr=self.stderr_tail))
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
            # Synchronous, before the task starts: ordinals, phases and refusals follow line order.
            issued = (
                self._issue(msg)
                if method == "agent"
                else self._issue_gate(msg)
                if method == "gate"
                else self._issue_verb(msg)
                if method == "verb"
                else None
            )
            task = asyncio.create_task(self._on_request(msg, issued))
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)
        else:
            self._on_notification(str(method), msg.get("params"))

    def _issue(
        self, msg: Mapping[str, Any]
    ) -> tuple[AgentCall, AgentRecord | None] | Exception | None:
        """Take the call's ordinal at the host; a refusal is returned, not raised."""
        state = self._run
        if state is None:
            return None
        scheduler = state.scheduler
        try:
            params = msg.get("params")
            params = params if isinstance(params, Mapping) else {}
            phase, label = params.get("phase"), params.get("label")
            return scheduler.issue(
                str(params["prompt"]),
                dict(params.get("opts") or {}),
                phase=phase if isinstance(phase, str) else None,
                label=label if isinstance(label, str) else None,
            )
        except Exception as exc:
            return exc

    def _issue_gate(
        self, msg: Mapping[str, Any]
    ) -> tuple[GateCall, GateRecord | None] | Exception | None:
        """Take the gate's ordinal at the host; a malformed request is returned, not raised."""
        state = self._run
        if state is None:
            return None
        params = msg.get("params")
        params = params if isinstance(params, Mapping) else {}
        try:
            return state.gates.issue(
                params.get("kind"), params.get("question"), params.get("options")
            )
        except Exception as exc:
            return exc

    def _issue_verb(
        self, msg: Mapping[str, Any]
    ) -> tuple[VerbCall, VerbRecord | None] | Exception | None:
        """Take the verb's ordinal at the host; a refused request is returned, not raised."""
        state = self._run
        if state is None:
            return None
        params = msg.get("params")
        params = params if isinstance(params, Mapping) else {}
        try:
            return state.verbs.issue(params.get("name"), params.get("args"))
        except Exception as exc:
            return exc

    async def _on_request(self, msg: Mapping[str, Any], issued: _Issued = None) -> None:
        method = msg.get("method")
        if method in ("agent", "workflow", "gate", "verb"):
            task = asyncio.current_task()
            if task is not None:
                self._agent_tasks.add(task)
            try:
                if method == "agent":
                    await self._on_agent(msg, issued)
                elif method == "gate":
                    await self._on_gate(msg, issued)
                elif method == "verb":
                    await self._on_verb(msg, issued)
                else:
                    await self._on_workflow(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A bug here (a raising listener, say) must not leave the
                # script waiting on a reply that never comes.
                with contextlib.suppress(Exception):
                    await self._write_error(
                        msg["id"], protocol.INTERNAL_ERROR, f"{method}() failed in the host: {exc}"
                    )
            finally:
                if task is not None:
                    self._agent_tasks.discard(task)
            return
        await self._write_error(
            msg["id"], protocol.METHOD_NOT_FOUND, f"method not found: {msg['method']}"
        )

    async def _write_error(
        self,
        request_id: Any,
        code: int,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._write({"jsonrpc": protocol.JSONRPC, "id": request_id, "error": error})

    async def _on_agent(self, msg: Mapping[str, Any], issued: _Issued) -> None:
        state = self._run
        if state is None or issued is None:
            await self._write_error(msg["id"], protocol.INTERNAL_ERROR, "no run is active")
            return
        scheduler = state.scheduler
        try:
            if isinstance(issued, Exception):
                raise issued
            call, cached = issued
            if not isinstance(call, AgentCall) or isinstance(cached, GateRecord | VerbRecord):
                raise TypeError("not an agent request")
            value = await scheduler.run(call, cached)
        except SchedulerRefusal as exc:
            code = (
                protocol.AGENT_LIMIT
                if isinstance(exc, AgentLimitError)
                else protocol.BUDGET_EXHAUSTED
            )
            await self._write_error(msg["id"], code, str(exc), {"spent": scheduler.budget.spent})
            return
        await self._write(
            {
                "jsonrpc": protocol.JSONRPC,
                "id": msg["id"],
                "result": {"value": value, "spent": scheduler.budget.spent},
            }
        )

    async def _on_gate(self, msg: Mapping[str, Any], issued: _Issued) -> None:
        state = self._run
        if state is None or issued is None:
            await self._write_error(msg["id"], protocol.INTERNAL_ERROR, "no run is active")
            return
        if isinstance(issued, Exception):
            await self._write_error(msg["id"], protocol.INVALID_PARAMS, str(issued))
            return
        call, cached = issued
        if not isinstance(call, GateCall) or isinstance(cached, AgentRecord | VerbRecord):
            await self._write_error(msg["id"], protocol.INTERNAL_ERROR, "not a gate request")
            return
        try:
            value = await state.gates.run(call, cached)
        except GateUnavailableError as exc:
            await self._write_error(msg["id"], protocol.GATE_ERROR, str(exc))
            return
        await self._write(
            {"jsonrpc": protocol.JSONRPC, "id": msg["id"], "result": {"value": value}}
        )

    async def _on_verb(self, msg: Mapping[str, Any], issued: _Issued) -> None:
        state = self._run
        if state is None or issued is None:
            await self._write_error(msg["id"], protocol.INTERNAL_ERROR, "no run is active")
            return
        if isinstance(issued, Exception):
            await self._write_error(msg["id"], protocol.INVALID_PARAMS, str(issued))
            return
        call, cached = issued
        if not isinstance(call, VerbCall) or isinstance(cached, (AgentRecord, GateRecord)):
            await self._write_error(msg["id"], protocol.INTERNAL_ERROR, "not a verb request")
            return
        try:
            value = await state.verbs.run(call, cached)
        except VerbFailedError as exc:
            await self._write_error(msg["id"], protocol.VERB_ERROR, str(exc))
            return
        await self._write(
            {"jsonrpc": protocol.JSONRPC, "id": msg["id"], "result": {"value": value}}
        )

    async def _on_workflow(self, msg: Mapping[str, Any]) -> None:
        state = self._run
        if state is None:
            await self._write_error(msg["id"], protocol.INTERNAL_ERROR, "no run is active")
            return
        if self._child_connection:
            await self._write_error(msg["id"], protocol.WORKFLOW_ERROR, protocol.NEST_MESSAGE)
            return
        params = msg.get("params") or {}
        result: dict[str, Any]
        try:
            # A resumed run reads children from the snapshot, never the filesystem.
            resolved = await resolve_for_run(state, params.get("ref"))
            if language_of(resolved.filename) == self._language:
                result = {"source": resolved.source, "filename": resolved.filename}
            else:
                value = await run_workflow_child(state, resolved, params.get("args", NO_ARGS))
                result = {"value": value}
        except (WorkflowRefError, WorkflowRunError, JournalError) as exc:
            await self._write_error(msg["id"], protocol.WORKFLOW_ERROR, str(exc))
            return
        result["spent"] = state.scheduler.budget.spent
        await self._write({"jsonrpc": protocol.JSONRPC, "id": msg["id"], "result": result})

    def _on_notification(self, method: str, params: Any) -> None:
        if not isinstance(params, Mapping):
            return
        state = self._run
        if state is None:
            return
        if method == "log" and state.on_log is not None:
            # Masked here, so every caller of the sandbox (provider, CLI, library) gets the
            # same masked line; masking twice changes nothing.
            message = str(params.get("message", ""))
            mask = state.options.redact
            state.on_log(str(mask(message)) if mask is not None else message)
        elif method == "canvas":
            task = asyncio.create_task(state.gates.canvas(params.get("url"), params.get("title")))
            self._canvas_tasks.add(task)
            task.add_done_callback(self._canvas_tasks.discard)
        elif method == "phase":
            title = str(params.get("title", ""))
            state.scheduler.set_phase(title)
            if state.on_phase is not None:
                state.on_phase(title)

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

    async def _run_child(self, state: RunState, resolved: ResolvedScript, args: object) -> Any:
        """Run `resolved` as a child of `state`'s run; the parent's timeout governs."""
        self._child_connection = True
        self._run = state
        try:
            params: dict[str, Any] = {
                "source": resolved.source,
                "filename": resolved.filename,
                "limits": {
                    "maxAgents": state.options.max_agents,
                    "budgetTotal": state.options.budget_total,
                    "maxItems": MAX_ITEMS_PER_CALL,
                },
                "child": True,
                "spent": state.scheduler.budget.spent,
            }
            if state.verbs.names:
                params["verbs"] = list(state.verbs.names)
            if args is not NO_ARGS:
                params["args"] = args
            result = await self._request("run", params)
            return result["value"]
        finally:
            self._run = None

    def _check_language(self, filename: str) -> None:
        if language_of(filename) != self._language:
            kind = "Python" if self._language == "py" else "JavaScript"
            raise SandboxError(f"{filename} is not a {kind} workflow")

    async def run(
        self,
        source: str | None,
        *,
        dispatcher: Dispatcher,
        args: object = NO_ARGS,
        filename: str = "workflow.js",
        options: RunOptions | None = None,
        abort: AbortSignal | None = None,
        on_log: Callable[[str], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
        on_agent_start: Callable[[AgentCall], None] | None = None,
        on_agent_end: Callable[[AgentCall, Any], None] | None = None,
        on_run_start: Callable[[str], None] | None = None,
        gates: Gates | None = None,
        verb_host: VerbHost | None = None,
    ) -> Any:
        """Run a script; `agent()` calls go through a Scheduler to `dispatcher`.

        With `options.state_dir` the run is journaled; `source=None` resumes
        `options.resume_from` from its snapshot. `on_run_start` gets the run id
        before the script starts.
        """
        async with self._run_lock:
            options = options or RunOptions()
            # Before the journal opens, so a refused run leaves none behind.
            if source is not None:
                self._check_language(filename)
            elif options.state_dir is not None and options.resume_from:
                self._check_language(
                    load_journal(options.state_dir, options.resume_from).header.filename
                )
            jr: JournalRun | None = None
            if options.state_dir is not None:
                jr = JournalRun.open(
                    options,
                    source=source,
                    filename=filename,
                    has_args=args is not NO_ARGS,
                    args=None if args is NO_ARGS else args,
                    on_log=on_log,
                )
                source, filename, options = jr.source, jr.filename, jr.options
                args = jr.args if jr.has_args else NO_ARGS
            elif source is None:
                raise ValueError("a script source is needed unless resuming")
            try:
                self._check_language(filename)
            except SandboxError:
                if jr is not None:
                    jr.close()
                raise
            try:
                scheduler = Scheduler(
                    dispatcher,
                    options,
                    abort=abort,
                    on_log=on_log,
                    on_agent_start=on_agent_start,
                    on_agent_end=on_agent_end,
                    journal=jr.writer if jr else None,
                    replay=jr.replay if jr else None,
                )
            except BaseException:
                if jr is not None:
                    jr.close()
                raise
            gate_runner = GateRunner(
                gates,
                abort=abort,
                mask=options.redact,
                on_log=on_log,
                journal=jr.writer if jr else None,
                replay=jr.gate_replay if jr else None,
            )
            verb_runner = VerbRunner(
                options.verbs,
                host=verb_host,
                abort=abort,
                mask=options.redact,
                on_log=on_log,
                journal=jr.writer if jr else None,
                replay=jr.verb_replay if jr else None,
                run_id=jr.run_id if jr else None,
                phase_of=lambda: scheduler.phase,
            )
            state = RunState(
                scheduler=scheduler,
                options=options,
                registry=Registry(
                    options.workflow_dirs,
                    python_dirs=options.python_dirs,
                    client=options.client_workflows,
                ),
                journal=jr,
                node=self._node,
                on_log=on_log,
                on_phase=on_phase,
                gates=gate_runner,
                verbs=verb_runner,
            )
            self._run = state
            try:
                if jr is not None and on_run_start is not None:
                    on_run_start(jr.run_id)
                params: dict[str, Any] = {
                    "source": source,
                    "filename": filename,
                    "limits": {
                        "maxAgents": options.max_agents,
                        "budgetTotal": options.budget_total,
                        "maxItems": MAX_ITEMS_PER_CALL,
                    },
                }
                if args is not NO_ARGS:
                    params["args"] = args
                if verb_runner.names:
                    params["verbs"] = list(verb_runner.names)
                try:
                    async with asyncio.timeout(options.timeout):
                        result = await self._request("run", params)
                except SandboxError as exc:
                    _masked(exc, options.redact)
                    raise
                except TimeoutError:
                    # A busy loop blocks Node's event loop, so only a kill stops it.
                    error = RunTimeoutError(
                        f"run timed out after {options.timeout:g} s", stderr=self.stderr_tail
                    )
                    scheduler.abort()
                    self._kill(error)
                    state.kill_children(error)
                    raise error from None
                return result["value"]
            except asyncio.CancelledError:
                scheduler.abort()
                cancelled = SandboxError("run cancelled", stderr=self.stderr_tail)
                self._kill(cancelled)
                state.kill_children(cancelled)
                raise
            finally:
                scheduler.close()
                state.kill_children(SandboxError("run ended", stderr=self.stderr_tail))
                self._run = None
                canvases = list(self._canvas_tasks)
                if canvases:
                    _, stuck = await asyncio.wait(canvases, timeout=CANVAS_FLUSH_TIMEOUT)
                    for canvas_task in stuck:
                        canvas_task.cancel()
                    await asyncio.gather(*stuck, return_exceptions=True)
                state.gates.close()
                verb_runner.close()
                tasks = list(self._agent_tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if jr is not None:
                    jr.close()


async def extract_meta(
    source: str, *, node: str | None, filename: str = "workflow.js"
) -> dict[str, Any] | None:
    """Read `meta` from a script: in-process for `.py`, else with a throwaway sandbox."""
    if language_of(filename) == "py":
        from ahp_host_flow.sandbox import pyrunner

        try:
            return pyrunner.read_meta(source, filename)
        except pyrunner.RpcError as exc:
            raise _error_from({"code": exc.code, "message": exc.message}, "") from None
    async with Sandbox(node) as sandbox:
        return await sandbox.meta(source, filename=filename)


async def run_script(
    source: str | None,
    *,
    node: str | None,
    dispatcher: Dispatcher,
    args: object = NO_ARGS,
    filename: str = "workflow.js",
    options: RunOptions | None = None,
    abort: AbortSignal | None = None,
    on_log: Callable[[str], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    on_agent_start: Callable[[AgentCall], None] | None = None,
    on_agent_end: Callable[[AgentCall, Any], None] | None = None,
    on_run_start: Callable[[str], None] | None = None,
    gates: Gates | None = None,
    verb_host: VerbHost | None = None,
) -> Any:
    """Run a script with a throwaway sandbox; Node for `.js`, the Python runner for `.py`."""
    name = filename
    if source is None and options is not None and options.state_dir and options.resume_from:
        name = load_journal(options.state_dir, options.resume_from).header.filename
    async with Sandbox(node, language=language_of(name)) as sandbox:
        return await sandbox.run(
            source,
            dispatcher=dispatcher,
            args=args,
            filename=filename,
            options=options,
            abort=abort,
            on_log=on_log,
            on_phase=on_phase,
            on_agent_start=on_agent_start,
            on_agent_end=on_agent_end,
            on_run_start=on_run_start,
            gates=gates,
            verb_host=verb_host,
        )
