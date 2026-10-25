"""The client methods an ACP agent may call: `fs/*` and `terminal/*`, served by the host.

Offered only when the agent's spec has `client_methods` (config
`[policy] host_tools = true`), so the agent reads, writes and runs commands
*through the host*, where they can be judged. (The internal name is not
`host_tools`: that already means the per-turn tools the host gives a turn.)

Every request is judged in this order, and the order is the point:

1. the roots jail: a path (a terminal's `cwd` included) that resolves outside
   the served folders is refused, and no policy setting relaxes that, not even
   `allow_paths_outside_roots`;
2. the host's `ToolPolicy`, when there is one: *deny* is an error whose message
   is the policy's reason (the agent shows it to its model), *ask* puts the call
   to the turn's user through the sink, and a declined call is an error.

An agent that ignores these methods and uses its own file and shell tools is
not constrained by any of this: pair this with a launcher and `env_allow`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeGuard

from ahp_acp import AcpError
from ahp_host.provider.base import ToolConfirmation, TurnSink
from ahp_host.provider.policy import ToolPolicy, ToolRequest
from ahp_host.provider.spawn import child_env

from ahp_host_acp.roots import Roots

METHODS: Final = frozenset(
    {
        "fs/read_text_file",
        "fs/write_text_file",
        "terminal/create",
        "terminal/output",
        "terminal/wait_for_exit",
        "terminal/kill",
        "terminal/release",
    }
)
#: `PermissionDenied` (`ahp_protocol` error table), as `provider.PERMISSION_DENIED`.
DENIED: Final = -32009
#: ACP's "resource not found".
NOT_FOUND: Final = -32002
INVALID: Final = -32602
#: Bytes of output kept when the agent gives no `outputByteLimit`, and the most
#: it may ask for: a host-side bound.
OUTPUT_LIMIT: Final = 1024 * 1024
#: Seconds a reaped terminal's reader gets to read what is left to EOF, and its
#: process to be waited for, before either is given up on (a child that left the
#: terminal's group can hold the output pipe open for as long as it runs).
REAP_GRACE: Final = 1.0
_KINDS: Final = {"fs/read_text_file": "read", "fs/write_text_file": "write"}


@dataclass(frozen=True)
class ExtRequestContext:
    """What a handler of an agent's extension request (`AgentSpec.ext_handlers`) is given.

    `cwd` and `roots` are the folders the host granted this session; `resolve_path`
    is the same jail `fs/*` is served under (it raises `AcpError` for a relative
    path or one outside `roots`, and returns the resolved path otherwise), so a
    handler never needs its own. `policy` is the host's `ToolPolicy`, if any,
    for a handler that wants to judge what it is about to do.
    """

    method: str
    session_id: str | None
    cwd: Path
    roots: Roots
    policy: ToolPolicy | None
    resolve_path: Callable[[Any], Path]
    log: logging.Logger


#: Answers one extension request (a method starting with `_`) from the agent.
ExtHandler = Callable[[ExtRequestContext, dict[str, Any]], Awaitable[dict[str, Any]]]
#: The message of the JSON-RPC error a failing handler is answered with.
EXT_FAILED: Final = "extension request failed"
INTERNAL: Final = -32603
_log = logging.getLogger("ahp_host_acp.ext")


@dataclass
class _Terminal:
    process: asyncio.subprocess.Process
    limit: int
    buffer: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    reader: asyncio.Task[None] | None = None

    async def read(self) -> None:
        stream = self.process.stdout
        assert stream is not None
        while chunk := await stream.read(65536):
            self.buffer += chunk
            if len(self.buffer) > self.limit:
                del self.buffer[: len(self.buffer) - self.limit]
                self.truncated = True
                # Start on a character boundary: drop UTF-8 continuation bytes.
                while self.buffer and 0x80 <= self.buffer[0] <= 0xBF:
                    del self.buffer[0]

    def status(self) -> dict[str, Any] | None:
        code = self.process.returncode
        if code is None:
            return None
        if code < 0:
            return {"exitCode": None, "signal": signal.Signals(-code).name}
        return {"exitCode": code, "signal": None}

    async def kill(self) -> None:
        """Kill the command and everything it started.

        As `ahp_acp.connection` does for an agent: on POSIX the command leads
        its own session (`start_new_session`), so the whole group is killed,
        even after the leader exited and left a grandchild behind; on Windows
        `taskkill /T` takes the tree while the leader still runs.
        """
        if sys.platform == "win32":
            if self.process.returncode is None:
                await _taskkill(self.process.pid)
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGKILL)


async def _taskkill(pid: int) -> None:
    with contextlib.suppress(OSError):
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/T",
            "/F",
            "/PID",
            str(pid),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        await killer.wait()


class ClientMethods:
    """Serves the `fs/*` and `terminal/*` requests of one session's agent."""

    def __init__(
        self,
        *,
        roots: Roots,
        policy: ToolPolicy | None,
        env_allow: Sequence[str] | None,
        cwd: Callable[[], Path],
    ) -> None:
        self._roots = roots
        self._policy = policy
        self._env_allow = env_allow
        self._cwd = cwd
        self._terminals: dict[str, _Terminal] = {}
        self._counter = 0

    async def handle(self, method: str, params: Mapping[str, Any], sink: TurnSink | None) -> Any:
        if method in ("fs/read_text_file", "fs/write_text_file"):
            resolved = self._jailed(params.get("path"))
            await self._judge(method, params, sink, request_input={"paths": (str(resolved),)})
            if method == "fs/read_text_file":
                return await self._read(resolved, params)
            return await self._write(resolved, params)
        if method == "terminal/create":
            return await self._create(params, sink)
        terminal = self._terminal(params)
        if method == "terminal/output":
            reply: dict[str, Any] = {
                "output": terminal.buffer.decode("utf-8", "replace"),
                "truncated": terminal.truncated,
            }
            if (status := terminal.status()) is not None:
                reply["exitStatus"] = status
            return reply
        if method == "terminal/wait_for_exit":
            await terminal.process.wait()
            if terminal.reader is not None:
                # Not `await reader`: a release or the session's end may cancel
                # the reader meanwhile, and the agent's wait is answered anyway.
                await asyncio.wait({terminal.reader})
            return terminal.status()
        if method == "terminal/kill":
            await terminal.kill()
            return {}
        if method == "terminal/release":
            await terminal.kill()
            self._terminals.pop(str(params.get("terminalId")), None)
            await self._reap(terminal)
            return {}
        raise AcpError(f"unknown client method {method}", INVALID)

    async def handle_ext(
        self, handler: ExtHandler, method: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Run an embedder's extension handler; any failure is a generic JSON-RPC error."""
        session_id = params.get("sessionId")
        context = ExtRequestContext(
            method=method,
            session_id=session_id if isinstance(session_id, str) else None,
            cwd=self._cwd(),
            roots=self._roots,
            policy=self._policy,
            resolve_path=self._jailed,
            log=_log,
        )
        try:
            return await handler(context, dict(params))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("extension request %s failed", method)
            raise AcpError(EXT_FAILED, INTERNAL) from None

    async def aclose(self) -> None:
        """Kill and forget every terminal."""
        terminals, self._terminals = list(self._terminals.values()), {}
        for terminal in terminals:
            await terminal.kill()
        for terminal in terminals:
            await self._reap(terminal)

    # -- the jail and the policy ------------------------------------------------

    def _jailed(self, path: Any) -> Path:
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise AcpError("path must be absolute", INVALID)
        resolved = Path(path).resolve()
        if not self._roots.contains(resolved):
            raise AcpError(f"{resolved} is outside this host's folders", DENIED)
        return resolved

    async def _judge(
        self,
        method: str,
        params: Mapping[str, Any],
        sink: TurnSink | None,
        *,
        request_input: Mapping[str, Any],
    ) -> None:
        if self._policy is None:
            return
        decision = self._policy.decide(
            ToolRequest(
                name=method,
                kind=_KINDS.get(method, "command"),
                input=request_input,
                cwd=self._cwd(),
                roots=tuple(self._roots.paths),
                raw=dict(params),
            )
        )
        if decision.action == "allow":
            return
        reason = decision.reason or f"{method} is not allowed"
        if decision.action == "deny":
            raise AcpError(reason, DENIED)
        if sink is None:
            raise AcpError(reason + " (no one to ask)", DENIED)
        call_id = f"host-{uuid.uuid4().hex}"
        display = {key: value for key, value in params.items() if key != "content"}
        await sink.tool_call_started(call_id, method, display, display_name=method)
        outcome = await sink.confirm_tool_call(
            ToolConfirmation(
                call_id=call_id,
                name=method,
                display_name=method,
                invocation_message=reason,
                tool_input=display,
            )
        )
        verdict = "allowed" if outcome.approved else "declined"
        await sink.tool_call_completed(
            call_id,
            {"content": [{"type": "text", "text": verdict}]},
            success=outcome.approved,
            past_tense_message=f"{method}: {verdict}",
        )
        if not outcome.approved:
            raise AcpError(f"declined: {reason}", DENIED)

    # -- fs ------------------------------------------------------------------------

    async def _read(self, resolved: Path, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            text = await asyncio.to_thread(resolved.read_bytes)
        except (FileNotFoundError, NotADirectoryError):
            raise AcpError(f"{resolved}: no such file", NOT_FOUND) from None
        except (IsADirectoryError, PermissionError) as exc:
            raise AcpError(f"{resolved}: {exc.strerror or exc}", INVALID) from None
        content = text.decode("utf-8", "replace")
        line, limit = params.get("line"), params.get("limit")
        if line is not None or limit is not None:
            lines = content.splitlines(keepends=True)
            start = max(line - 1, 0) if _is_int(line) else 0
            end = start + max(limit, 0) if _is_int(limit) else None
            content = "".join(lines[start:end])
        return {"content": content}

    async def _write(self, resolved: Path, params: Mapping[str, Any]) -> dict[str, Any]:
        content = params.get("content")
        if not isinstance(content, str):
            raise AcpError("content must be a string", INVALID)

        def write() -> None:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_bytes(content.encode("utf-8"))

        try:
            await asyncio.to_thread(write)
        except OSError as exc:
            raise AcpError(f"{resolved}: {exc.strerror or exc}", INVALID) from None
        return {}

    # -- terminals -------------------------------------------------------------------

    async def _create(self, params: Mapping[str, Any], sink: TurnSink | None) -> dict[str, Any]:
        command = params.get("command")
        if not isinstance(command, str) or not command:
            raise AcpError("command is required", INVALID)
        args = params.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise AcpError("args must be a list of strings", INVALID)
        env: dict[str, str] = {}
        for item in params.get("env", []):
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                raise AcpError("env must be a list of {name, value}", INVALID)
            env[item["name"]] = str(item.get("value", ""))
        limit = params.get("outputByteLimit", OUTPUT_LIMIT)
        if not _is_int(limit) or limit < 0:
            raise AcpError("outputByteLimit must be a number", INVALID)
        limit = min(limit, OUTPUT_LIMIT)
        where = self._jailed(params["cwd"]) if "cwd" in params else self._cwd()
        await self._judge(
            "terminal/create", params, sink, request_input={"command": shlex.join([command, *args])}
        )
        environment = (
            {**os.environ, **env} if self._env_allow is None else child_env(self._env_allow, env)
        )
        # Its own process group, so kill and release reach what it starts.
        grouping: dict[str, Any] = {"start_new_session": True}
        if sys.platform == "win32":
            grouping = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            }
        try:
            process = await asyncio.create_subprocess_exec(
                shutil.which(command) or command,
                *args,
                cwd=where,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **grouping,
            )
        except FileNotFoundError:
            raise AcpError(f"{command}: not found", NOT_FOUND) from None
        except OSError as exc:
            raise AcpError(f"{command}: {exc.strerror or exc}", INVALID) from None
        terminal = _Terminal(process, limit)
        terminal.reader = asyncio.create_task(terminal.read())
        self._counter += 1
        terminal_id = f"term-{self._counter}"
        self._terminals[terminal_id] = terminal
        return {"terminalId": terminal_id}

    def _terminal(self, params: Mapping[str, Any]) -> _Terminal:
        terminal = self._terminals.get(str(params.get("terminalId")))
        if terminal is None:
            raise AcpError("unknown terminal", INVALID)
        return terminal

    @staticmethod
    async def _reap(terminal: _Terminal) -> None:
        """After a kill: let the reader reach EOF (a waiting `terminal/wait_for_exit`
        then has all the output), but not for longer than `REAP_GRACE`."""
        reader = terminal.reader
        if reader is not None:
            await asyncio.wait({reader}, timeout=REAP_GRACE)
            if not reader.done():
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
        with contextlib.suppress(Exception):
            await asyncio.wait_for(terminal.process.wait(), REAP_GRACE)


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)
