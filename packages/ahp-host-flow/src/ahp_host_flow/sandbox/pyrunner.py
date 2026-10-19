"""The Python script runner: `python -I -m ahp_host_flow.sandbox.pyrunner`.

Speaks the same newline-delimited JSON-RPC as `runner.mjs` (see `protocol.py`) and
gives a `.py` workflow the same verbs as globals. It imports only the standard
library and `protocol`: never `ahp_host`, `ahp_client`, `ahp_protocol`, the engine,
`client.py` or third-party code (an import-linter contract checks this).

The protocol runs on private copies of the real stdin and stdout; fds 0 and 1 are
re-pointed at devnull and stderr, so a script's `print()` and any C write land on
stderr. Output lines are ASCII-only and end in "\\n" whatever the locale or platform.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextvars
import inspect
import json
import math
import os
import re
import sys
import threading
import traceback
from collections.abc import Callable, Coroutine, Mapping, Sequence
from typing import IO, Any, Final

from ahp_host_flow.sandbox import protocol

MISSING: Final = object()
META_ERROR: Final = protocol.META_ERROR


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


SCRIPT_CANCELLED: Final = "the script was cancelled"


class ScriptCancelledError(Exception):
    """A CancelledError the script itself raised (it awaited a task it cancelled)."""


class _ExitError(Exception):
    """A SystemExit from a script, carried through a task (which would re-raise it)."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


# --- meta ---------------------------------------------------------------------


def _binds_meta(target: ast.expr) -> list[ast.expr]:
    if isinstance(target, ast.Name):
        return [target] if target.id == "meta" else []
    if isinstance(target, ast.Starred):
        return _binds_meta(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return [hit for el in target.elts for hit in _binds_meta(el)]
    return []


def _meta_bindings(stmt: ast.stmt) -> list[int]:
    """The line of every binding of the name `meta` that this top-level statement makes."""
    if isinstance(stmt, ast.Assign):
        return [stmt.lineno for target in stmt.targets for _ in _binds_meta(target)]
    if isinstance(stmt, ast.AugAssign | ast.AnnAssign):
        return [stmt.lineno] if _binds_meta(stmt.target) else []
    if isinstance(stmt, ast.Import | ast.ImportFrom):
        return [
            stmt.lineno
            for alias in stmt.names
            if (alias.asname or alias.name.split(".")[0]) == "meta"
        ]
    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [stmt.lineno] if stmt.name == "meta" else []
    return []


def _json_like(obj: Any, path: str, line: int) -> Any:
    def bad(what: str) -> RpcError:
        return RpcError(META_ERROR, f"meta is not JSON-like: {path} {what} (line {line})")

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, val in obj.items():
            if not isinstance(key, str):
                raise bad("has a non-string key")
            out[key] = _json_like(val, f"{path}.{key}", line)
        return out
    if isinstance(obj, tuple | list):
        return [_json_like(val, f"{path}[{i}]", line) for i, val in enumerate(obj)]
    if obj is None or isinstance(obj, str | bool | int):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise bad("is a non-finite float")
        return obj
    raise bad(f"is a {type(obj).__name__}")


def read_meta(source: str, filename: str = "workflow.py") -> dict[str, Any] | None:
    """The workflow's `meta` dict, read from the source without executing anything."""
    try:
        tree = ast.parse(source, filename)
    except SyntaxError as exc:
        raise RpcError(
            protocol.SYNTAX_ERROR, f"{exc.msg} (line {exc.lineno})", {"filename": filename}
        ) from None
    except (ValueError, RecursionError, MemoryError) as exc:
        raise RpcError(
            protocol.SYNTAX_ERROR, str(exc) or type(exc).__name__, {"filename": filename}
        ) from None
    bound: list[tuple[int, ast.stmt]] = [
        (line, stmt) for stmt in tree.body for line in _meta_bindings(stmt)
    ]
    if not bound:
        return None
    once = "meta must be assigned once, as a literal (line {})"
    if len(bound) > 1:
        raise RpcError(META_ERROR, once.format(bound[1][0]))
    line, stmt = bound[0]
    value: ast.expr | None = None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
        if isinstance(target, ast.Name) and target.id == "meta":
            value = stmt.value
    elif (
        isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and stmt.target.id == "meta"
    ):
        value = stmt.value
    if value is None:
        raise RpcError(META_ERROR, once.format(line))
    if not isinstance(value, ast.Dict):
        raise RpcError(META_ERROR, f"meta must be a dict literal (line {value.lineno})")
    try:
        literal = ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        raise RpcError(META_ERROR, f"meta is not a pure literal (line {value.lineno})") from None
    result: dict[str, Any] = _json_like(literal, "meta", value.lineno)
    return result


# --- the verbs ----------------------------------------------------------------


def failed(exc: BaseException) -> asyncio.Future[Any]:
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    fut.set_exception(exc)
    return fut


def _done(value: Any) -> asyncio.Future[Any]:
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    fut.set_result(value)
    return fut


async def _swallow(awaitable: Any) -> Any:
    try:
        return await awaitable
    except Exception:
        return None


async def _gather(parts: list[asyncio.Future[Any]]) -> list[Any]:
    return list(await asyncio.gather(*parts))


def run_parallel(thunks: object, max_items: int) -> asyncio.Future[list[Any]]:
    """`parallel()`: call every thunk now, await all; a failing thunk gives None."""
    if not isinstance(thunks, list | tuple):
        return failed(TypeError("parallel() needs an array of functions"))
    if len(thunks) > max_items:
        return failed(ValueError(f"parallel() takes at most {max_items} items, got {len(thunks)}"))
    parts: list[asyncio.Future[Any]] = []
    for thunk in thunks:
        try:
            result = thunk()
        except Exception:
            parts.append(_done(None))
            continue
        if inspect.isawaitable(result):
            parts.append(asyncio.ensure_future(_swallow(result)))
        else:
            parts.append(_done(result))
    return asyncio.ensure_future(_gather(parts))


async def _advance(first: Any, rest: Sequence[Any], item: Any, index: int) -> Any:
    try:
        prev = await first if inspect.isawaitable(first) else first
        for stage in rest:
            prev = stage(prev, item, index)
            if inspect.isawaitable(prev):
                prev = await prev
    except Exception:
        return None
    return prev


def run_pipeline(
    items: object, stages: Sequence[object], max_items: int
) -> asyncio.Future[list[Any]]:
    """`pipeline()`: each item flows through the stages with no barrier between them."""
    if not isinstance(items, list | tuple):
        return failed(TypeError("pipeline() needs an array of items"))
    if len(items) > max_items:
        return failed(ValueError(f"pipeline() takes at most {max_items} items, got {len(items)}"))
    if not all(callable(stage) for stage in stages):
        return failed(TypeError("pipeline() stages must be functions"))
    if not stages:
        return _done(list(items))
    first_stage: Callable[..., Any] = stages[0]  # type: ignore[assignment]
    parts: list[asyncio.Future[Any]] = []
    for index, item in enumerate(items):
        try:
            first = first_stage(item, item, index)
        except Exception:
            parts.append(_done(None))
            continue
        parts.append(asyncio.ensure_future(_advance(first, stages[1:], item, index)))
    return asyncio.ensure_future(_gather(parts))


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def clone_json(value: Any) -> Any:
    """A JSON round trip, as `JSON.parse(JSON.stringify(v))` does it: a non-finite float is None."""
    return json.loads(json.dumps(value), parse_constant=lambda _name: None)


def _text(value: object) -> str:
    """`String(value)` for what a script logs: a non-finite float reads as JavaScript's."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    return str(value)


def _diff_ok(diff: object) -> bool:
    if isinstance(diff, str):
        return True
    if not isinstance(diff, (list, tuple)):
        return False

    def optional(value: object) -> bool:
        return value is None or isinstance(value, str)

    return all(
        isinstance(d, Mapping)
        and isinstance(d.get("path"), str)
        and optional(d.get("before"))
        and optional(d.get("after"))
        for d in diff
    )


class _Link:
    """The connection: numbered requests out, their responses in, notifications."""

    def __init__(self, out: IO[bytes]) -> None:
        self.out = out
        self.next_id = 1
        self.pending: dict[int, asyncio.Future[Any]] = {}

    def send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        self.out.write(line.encode("ascii") + b"\n")
        self.out.flush()

    def call(self, method: str, params: dict[str, Any]) -> asyncio.Future[Any]:
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        rid = self.next_id
        self.next_id += 1
        self.pending[rid] = fut
        self.send({"jsonrpc": protocol.JSONRPC, "id": rid, "method": method, "params": params})
        return fut

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": protocol.JSONRPC, "method": method, "params": params})

    def resolve(self, msg: dict[str, Any]) -> None:
        rid = msg.get("id")
        fut = self.pending.pop(rid, None) if isinstance(rid, int) else None
        if fut is not None and not fut.done():
            fut.set_result(msg)

    def drop_pending(self) -> None:
        for fut in self.pending.values():
            fut.cancel()
        self.pending.clear()


class _Budget:
    __slots__ = ("_rt",)

    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    @property
    def total(self) -> int | None:
        return self._rt.budget_total

    def spent(self) -> int:
        return self._rt.spent[0]

    def remaining(self) -> float:
        total = self._rt.budget_total
        return math.inf if total is None else max(0, total - self._rt.spent[0])


CURRENT: contextvars.ContextVar[Runtime] = contextvars.ContextVar("ahp_flow_runtime")


def current_runtime() -> Runtime | None:
    return CURRENT.get(None)


def _error_text(msg: Mapping[str, Any]) -> str:
    return str(msg["error"].get("message")) if isinstance(msg["error"], dict) else "error"


class Runtime:
    """The verbs of one namespace. A parent and its in-process child share `spent` and `link`."""

    def __init__(
        self,
        link: _Link,
        *,
        budget_total: int | None,
        max_items: int,
        args: Any,
        child: bool,
        spent: list[int],
        verbs: tuple[str, ...] = (),
    ) -> None:
        self.link = link
        self.budget_total = budget_total
        self.max_items = max_items
        self.args = args
        self.child = child
        self.spent = spent
        self.verbs = verbs
        self.budget = _Budget(self)

    def _mirror(self, value: object) -> None:
        number = _int(value)
        if number is not None and number > self.spent[0]:
            self.spent[0] = number

    def agent(self, prompt: str, **opts: Any) -> asyncio.Future[Any]:
        if not isinstance(prompt, str):
            return failed(TypeError("agent() needs a prompt string"))
        try:
            opts = clone_json(opts)
        except (TypeError, ValueError) as exc:
            return failed(TypeError(f"agent() opts must be JSON: {exc}"))
        total = self.budget_total
        if total is not None and self.spent[0] >= total:
            return failed(
                RuntimeError(f"budget exhausted: {self.spent[0]} of {total} output tokens spent")
            )
        phase = opts.get("phase")
        label = opts.get("label")
        reply = self.link.call(
            "agent",
            {
                "prompt": prompt,
                "opts": opts,
                "phase": phase if isinstance(phase, str) else None,
                "label": label if isinstance(label, str) else None,
            },
        )
        return asyncio.ensure_future(self._agent_result(reply))

    async def _agent_result(self, reply: asyncio.Future[Any]) -> Any:
        msg = await reply
        if msg.get("error") is not None:
            data = msg["error"].get("data") if isinstance(msg["error"], dict) else None
            self._mirror(data.get("spent") if isinstance(data, dict) else None)
            raise RuntimeError(_error_text(msg))
        result = msg.get("result")
        if not isinstance(result, dict):
            return None
        self._mirror(result.get("spent"))
        return result.get("value")

    def parallel(self, thunks: object) -> asyncio.Future[list[Any]]:
        return run_parallel(thunks, self.max_items)

    def pipeline(self, items: object, *stages: object) -> asyncio.Future[list[Any]]:
        return run_pipeline(items, stages, self.max_items)

    def phase(self, title: object) -> None:
        self.link.notify("phase", {"title": _text(title)})

    def log(self, message: object) -> None:
        self.link.notify("log", {"message": _text(message)})

    def ask(self, question: object, **opts: Any) -> asyncio.Future[Any]:
        if not isinstance(question, str):
            return failed(TypeError("ask() needs a question string"))
        choices, schema = opts.get("choices"), opts.get("schema")
        if choices is not None and not (
            isinstance(choices, (list, tuple))
            and len(choices) > 0
            and all(isinstance(c, str) for c in choices)
        ):
            return failed(TypeError("ask() choices must be a non-empty array of strings"))
        if schema is not None and not isinstance(schema, Mapping):
            return failed(TypeError("ask() schema must be an object"))
        if choices is not None and schema is not None:
            return failed(TypeError("ask() takes choices or schema, not both"))
        options: dict[str, Any] = {}
        if choices is not None:
            options["choices"] = list(choices)
        if schema is not None:
            options["schema"] = schema
        try:
            options = clone_json(options)
        except (TypeError, ValueError) as exc:
            return failed(TypeError(f"ask() opts must be JSON: {exc}"))
        reply = self.link.call("gate", {"kind": "ask", "question": question, "options": options})
        return asyncio.ensure_future(self._gate_result(reply))

    def approve(self, summary: object, **opts: Any) -> asyncio.Future[Any]:
        if not isinstance(summary, str):
            return failed(TypeError("approve() needs a summary string"))
        diff = opts.get("diff")
        if diff is not None and not _diff_ok(diff):
            return failed(
                TypeError("approve() diff must be a string or an array of {path, before?, after?}")
            )
        x = opts.get("x")
        if x is not None:
            if not isinstance(x, dict):
                return failed(TypeError("approve() x must be an object"))
            if "detail" in x and not isinstance(x["detail"], bool):
                return failed(TypeError("approve() x.detail must be a boolean"))
        detail = x is not None and x.get("detail") is True
        options: dict[str, Any] = {} if diff is None else {"diff": diff}
        if detail:
            options["x"] = {"detail": True}
        try:
            options = clone_json(options)
        except (TypeError, ValueError) as exc:
            return failed(TypeError(f"approve() opts must be JSON: {exc}"))
        reply = self.link.call("gate", {"kind": "approve", "question": summary, "options": options})
        return asyncio.ensure_future(self._approve_result(reply, detail))

    def verb(self, name: object, *args: Any) -> asyncio.Future[Any]:
        if not isinstance(name, str) or name not in self.verbs:
            return failed(NameError(f"name {name!r} is not defined: it is not a verb of this run"))
        try:
            wire_args = clone_json(list(args))
        except (TypeError, ValueError) as exc:
            return failed(TypeError(f"{name}() arguments must be JSON: {exc}"))
        reply = self.link.call("verb", {"name": name, "args": wire_args})
        return asyncio.ensure_future(self._gate_result(reply))

    def verb_function(self, name: str) -> Callable[..., asyncio.Future[Any]]:
        def function(*args: Any) -> asyncio.Future[Any]:
            return self.verb(name, *args)

        function.__name__ = function.__qualname__ = name
        return function

    async def _gate_result(self, reply: asyncio.Future[Any]) -> Any:
        msg = await reply
        if msg.get("error") is not None:
            raise RuntimeError(_error_text(msg))
        result = msg.get("result")
        return result.get("value") if isinstance(result, dict) else None

    async def _approve_result(self, reply: asyncio.Future[Any], detail: bool = False) -> Any:
        value = await self._gate_result(reply)
        return value if detail else value is True

    def canvas(self, url: object, **opts: Any) -> None:
        if not isinstance(url, str) or re.match(r"(?i)https?://\S", url) is None:
            raise TypeError("canvas() needs an http(s) URL")
        title = opts.get("title")
        if title is not None and not isinstance(title, str):
            raise TypeError("canvas() title must be a string")
        self.link.notify("canvas", {"url": url, "title": title})

    def workflow(self, ref: object, args: Any = MISSING) -> asyncio.Future[Any]:
        if self.child:
            return failed(RuntimeError(protocol.NEST_MESSAGE))
        wire: str | dict[str, str]
        if isinstance(ref, str):
            wire = ref
        elif isinstance(ref, Mapping) and isinstance(ref.get("scriptPath"), str):
            wire = {"scriptPath": ref["scriptPath"]}
        else:
            return failed(TypeError("workflow() needs a name or {scriptPath}"))
        params: dict[str, Any] = {"ref": wire}
        if args is not MISSING:
            try:
                args = clone_json(args)
            except (TypeError, ValueError) as exc:
                return failed(TypeError(f"workflow() args must be JSON: {exc}"))
            params["args"] = args
        reply = self.link.call("workflow", params)
        return asyncio.ensure_future(self._workflow_result(reply, args))

    async def _workflow_result(self, reply: asyncio.Future[Any], args: Any) -> Any:
        msg = await reply
        if msg.get("error") is not None:
            raise RuntimeError(_error_text(msg))
        result = msg.get("result")
        if not isinstance(result, dict):
            result = {}
        self._mirror(result.get("spent"))
        if "value" in result:
            return result["value"]
        filename = result.get("filename")
        if not isinstance(filename, str):
            filename = "workflow.py"
        source = result.get("source")
        if not isinstance(source, str):
            raise RuntimeError(f"workflow {filename}: bad source")
        return await self._run_child(source, filename, None if args is MISSING else args)

    async def _run_child(self, source: str, filename: str, args: Any) -> Any:
        try:
            read_meta(source, filename)
            code = _compile(source, filename)
        except RpcError as exc:
            raise RuntimeError(f"workflow {filename}: {exc.message}") from None
        child = Runtime(
            self.link,
            budget_total=self.budget_total,
            max_items=self.max_items,
            args=args,
            child=True,
            spent=self.spent,
            verbs=self.verbs,
        )
        ctx = contextvars.copy_context()
        ctx.run(CURRENT.set, child)
        ns = _namespace(child, filename)
        ctx.run(exec, code, ns)
        main = ns.get("main")
        if not inspect.iscoroutinefunction(main):
            raise RuntimeError(
                f"workflow {filename}: a Python workflow needs `async def main(args)`"
            )
        try:
            value = await asyncio.create_task(_call_main(main, args), context=ctx)
        except _ExitError as exc:
            raise exc.original from None
        try:
            return clone_json(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"workflow {filename}: return value is not JSON-serialisable: {exc}"
            ) from None


def _namespace(rt: Runtime, filename: str) -> dict[str, Any]:
    ns: dict[str, Any] = {
        "__name__": "__workflow__",
        "__file__": filename,
        "__builtins__": builtins,
        "agent": rt.agent,
        "parallel": rt.parallel,
        "pipeline": rt.pipeline,
        "phase": rt.phase,
        "log": rt.log,
        "ask": rt.ask,
        "approve": rt.approve,
        "canvas": rt.canvas,
        "budget": rt.budget,
        "workflow": rt.workflow,
        "args": rt.args,
    }
    # After the built-ins, so a verb may replace ask, approve or canvas.
    for name in rt.verbs:
        ns[name] = rt.verb_function(name)
    return ns


def _compile(source: str, filename: str) -> Any:
    try:
        return compile(source, filename, "exec", dont_inherit=True)
    except SyntaxError as exc:
        raise RpcError(
            protocol.SYNTAX_ERROR, f"{exc.msg} (line {exc.lineno})", {"filename": filename}
        ) from None
    except (ValueError, RecursionError, MemoryError) as exc:
        raise RpcError(
            protocol.SYNTAX_ERROR, str(exc) or type(exc).__name__, {"filename": filename}
        ) from None


async def _call_main(main: Callable[[Any], Coroutine[Any, Any, Any]], args: Any) -> Any:
    try:
        return await main(args)
    except SystemExit as exc:
        raise _ExitError(exc) from None
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise  # the runner itself is being cancelled (the host killed the run)
        # The script awaited a task it had cancelled (or similar): its own error, not ours.
        raise ScriptCancelledError(SCRIPT_CANCELLED) from None


# --- the run and the wire -------------------------------------------------------


def _script_error(exc: BaseException) -> RpcError:
    return RpcError(
        protocol.SCRIPT_ERROR,
        str(exc),
        {"name": type(exc).__name__, "stack": "".join(traceback.format_exception(exc))},
    )


async def _run(link: _Link, params: dict[str, Any]) -> dict[str, Any]:
    source = params.get("source")
    if not isinstance(source, str):
        raise RpcError(protocol.INVALID_PARAMS, "source must be a string")
    filename = params.get("filename")
    if not isinstance(filename, str):
        filename = "workflow.py"
    read_meta(source, filename)
    code = _compile(source, filename)
    raw = params.get("limits")
    limits: dict[str, Any] = raw if isinstance(raw, dict) else {}
    max_items = _int(limits.get("maxItems"))
    spent = _int(params.get("spent"))
    raw_verbs = params.get("verbs")
    verbs = tuple(v for v in raw_verbs if isinstance(v, str)) if isinstance(raw_verbs, list) else ()
    rt = Runtime(
        link,
        budget_total=_int(limits.get("budgetTotal")),
        max_items=protocol.DEFAULT_MAX_ITEMS if max_items is None else max_items,
        args=params.get("args"),
        child=params.get("child") is True,
        spent=[spent if spent is not None and spent >= 0 else 0],
        verbs=verbs,
    )
    ns = _namespace(rt, filename)
    ctx = contextvars.copy_context()
    ctx.run(CURRENT.set, rt)
    try:
        ctx.run(exec, code, ns)
        main = ns.get("main")
        if not inspect.iscoroutinefunction(main):
            raise RpcError(
                protocol.SCRIPT_ERROR,
                "a Python workflow needs `async def main(args)`",
                {"name": "TypeError", "stack": None},
            )
        value = await asyncio.create_task(_call_main(main, rt.args), context=ctx)
    except RpcError:
        raise
    except _ExitError as exc:
        raise _script_error(exc.original) from None
    except (Exception, SystemExit) as exc:
        raise _script_error(exc) from None
    try:
        return {"value": clone_json(value)}
    except (TypeError, ValueError) as exc:
        raise RpcError(
            protocol.SCRIPT_ERROR,
            f"return value is not JSON-serialisable: {exc}",
            {"name": None, "stack": None},
        ) from None


def _error_message(rid: object, error: RpcError) -> dict[str, Any]:
    body: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.data is not None:
        body["data"] = error.data
    return {"jsonrpc": protocol.JSONRPC, "id": rid, "error": body}


async def _serve(inp: IO[bytes], out: IO[bytes]) -> int:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    link = _Link(out)

    def read_lines() -> None:
        # A thread rather than connect_read_pipe, which does not work on Windows pipes.
        while True:
            line = inp.readline()
            loop.call_soon_threadsafe(queue.put_nowait, line or None)
            if not line:
                return

    threading.Thread(target=read_lines, daemon=True).start()
    active: asyncio.Task[None] | None = None

    async def run_request(rid: object, params: dict[str, Any]) -> None:
        try:
            reply: dict[str, Any] = {
                "jsonrpc": protocol.JSONRPC,
                "id": rid,
                "result": await _run(link, params),
            }
        except RpcError as exc:
            reply = _error_message(rid, exc)
        except Exception as exc:
            reply = _error_message(rid, RpcError(protocol.INTERNAL_ERROR, str(exc)))
        finally:
            link.drop_pending()
        link.send(reply)

    while True:
        line = await queue.get()
        if line is None:
            break
        if not line.strip():
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError as exc:
            link.send(_error_message(None, RpcError(protocol.PARSE_ERROR, f"invalid JSON: {exc}")))
            continue
        if not isinstance(msg, dict):
            link.send(
                _error_message(None, RpcError(protocol.PARSE_ERROR, "message is not a JSON object"))
            )
            continue
        method = msg.get("method")
        if not isinstance(method, str):
            link.resolve(msg)
            continue
        rid = msg.get("id")
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            if method == "meta":
                source = params.get("source")
                if not isinstance(source, str):
                    raise RpcError(protocol.INVALID_PARAMS, "source must be a string")
                filename = params.get("filename")
                result = {
                    "meta": read_meta(
                        source, filename if isinstance(filename, str) else "workflow.py"
                    )
                }
            elif method == "run":
                if active is not None and not active.done():
                    raise RpcError(protocol.RUN_ACTIVE, "a run is already active")
                active = asyncio.create_task(run_request(rid, params))
                continue
            else:
                raise RpcError(protocol.METHOD_NOT_FOUND, f"method not found: {method}")
        except RpcError as exc:
            if "id" in msg:
                link.send(_error_message(rid, exc))
            continue
        except Exception as exc:
            if "id" in msg:
                link.send(_error_message(rid, RpcError(protocol.INTERNAL_ERROR, str(exc))))
            continue
        if "id" in msg:
            link.send({"jsonrpc": protocol.JSONRPC, "id": rid, "result": result})
    if active is not None:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
    return 0


def main() -> int:
    in_fd = os.dup(0)
    out_fd = os.dup(1)
    os.dup2(2, 1)  # stray prints and C writes go to stderr
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    sys.stdout = sys.stderr
    sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115
    out = open(out_fd, "wb")  # noqa: SIM115
    inp = open(in_fd, "rb")  # noqa: SIM115
    return asyncio.run(_serve(inp, out))


if __name__ == "__main__":
    # Run the importable copy, so the contextvar lives in the module `verbs` sees.
    from ahp_host_flow.sandbox import pyrunner as _canonical

    raise SystemExit(_canonical.main())
