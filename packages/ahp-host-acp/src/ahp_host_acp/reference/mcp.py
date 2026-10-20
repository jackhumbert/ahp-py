"""A stdio MCP client for the reference agent (standard library only).

MCP over a child's stdio is newline-delimited JSON-RPC, the transport `relay.py` serves. The
client starts the server, initialises it, lists its tools and calls them; the server's own
requests are answered (`ping`) or refused, and its notifications are ignored. A server does not
inherit the agent's own settings or secret-shaped variables (`child_env`), and a call can be
given up on cancel.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from typing import IO, Any

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "ahp-acp-openai"
CLIENT_VERSION = "0.1.0"

_METHOD_NOT_FOUND = -32601
_EXITED = "the MCP server exited"


class McpError(Exception):
    pass


class McpCancelled(McpError):  # noqa: N818
    """The call was given up because the turn was cancelled."""


#: Parent variables an MCP server does not inherit: the agent's own settings (its API key among
#: them) and anything named like a secret. The entry's own `env` may still set any of them.
_PRIVATE_PREFIX = "AHP_OPENAI_"
_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
#: How often a cancellable wait looks at its cancel event, in seconds.
_CANCEL_POLL = 0.05


def child_env(parent: Mapping[str, str], explicit: Mapping[str, str]) -> dict[str, str]:
    """The environment for an MCP server: `parent` without private or secret-shaped names,
    then `explicit` (the ACP entry's `env`) on top."""
    env: dict[str, str] = {}
    for name, value in parent.items():
        upper = name.upper()
        if upper.startswith(_PRIVATE_PREFIX) or any(w in upper for w in _SECRET_WORDS):
            continue
        env[name] = value
    env.update(explicit)
    return env


class McpClient:
    def __init__(
        self, name: str, command: str, args: Sequence[str], env: Mapping[str, str]
    ) -> None:
        self.name = name
        self._command = command
        self._args = list(args)
        self._env = dict(env)
        self._proc: subprocess.Popen[bytes] | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._waiters: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._next_id = 1
        self._exited = False

    def start(self, timeout: float = 30.0) -> None:
        self._proc = subprocess.Popen(
            [self._command, *self._args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            env=child_env(os.environ, self._env),
        )
        assert self._proc.stdout is not None
        threading.Thread(target=self._read, args=(self._proc.stdout,), daemon=True).start()
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
            timeout,
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        while True:
            result = self._request("tools/list", params, timeout)
            listed = result.get("tools")
            if isinstance(listed, list):
                tools += [
                    t for t in listed if isinstance(t, dict) and isinstance(t.get("name"), str)
                ]
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return tools
            params = {"cursor": cursor}

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        cancel: threading.Event | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Call a tool. No timeout by default (a client tool may wait on a person); when `cancel`
        is set the call is given up at once (`McpCancelled`) and the server is told."""
        params = {"name": name, "arguments": dict(arguments)}
        return self._request("tools/call", params, timeout, cancel)

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        for stop in (None, proc.terminate, proc.kill):
            if stop is not None:
                stop()
            try:
                proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue
        if proc.stdout is not None:
            proc.stdout.close()

    def _wait(
        self,
        event: threading.Event,
        request_id: int,
        timeout: float | None,
        cancel: threading.Event | None,
    ) -> bool:
        """Wait for the answer; False on timeout, McpCancelled when `cancel` is set first."""
        if cancel is None:
            return event.wait(timeout)
        deadline = None if timeout is None else time.monotonic() + timeout
        while not event.is_set():
            if cancel.is_set():
                with contextlib.suppress(McpError):
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/cancelled",
                            "params": {"requestId": request_id, "reason": "cancelled"},
                        }
                    )
                raise McpCancelled("cancelled")
            step = _CANCEL_POLL
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                step = min(step, left)
            event.wait(step)
        return True

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        timeout: float | None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        event = threading.Event()
        slot: dict[str, Any] = {}
        with self._state_lock:
            if self._exited:
                raise McpError(_EXITED)
            request_id = self._next_id
            self._next_id += 1
            self._waiters[request_id] = (event, slot)
        try:
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
            )
            if not self._wait(event, request_id, timeout, cancel):
                raise McpError(f"{method} timed out")
        finally:
            with self._state_lock:
                self._waiters.pop(request_id, None)
        if "exited" in slot:
            raise McpError(_EXITED)
        error = slot.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else None
            raise McpError(message if isinstance(message, str) else json.dumps(error))
        result = slot.get("result")
        return result if isinstance(result, dict) else {}

    def _send(self, message: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError(_EXITED)
        data = (json.dumps(message, ensure_ascii=True) + "\n").encode("ascii")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except OSError as exc:
                raise McpError(_EXITED) from exc

    def _read(self, stdout: IO[bytes]) -> None:
        try:
            for line in stdout:
                try:
                    message = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._deliver(message)
        except (OSError, ValueError):
            pass
        with self._state_lock:
            self._exited = True
            waiters = list(self._waiters.values())
        for event, slot in waiters:
            slot["exited"] = True
            event.set()

    def _deliver(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            if "id" not in message:
                return
            if method == "ping":
                reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
            else:
                reply = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": _METHOD_NOT_FOUND, "message": f"method not found: {method}"},
                }
            with contextlib.suppress(McpError):
                self._send(reply)
            return
        with self._state_lock:
            waiter = self._waiters.get(message.get("id"))  # type: ignore[arg-type]
        if waiter is not None:
            event, slot = waiter
            slot.update(message)
            event.set()


def from_acp_entry(entry: Mapping[str, Any]) -> McpClient | None:
    """An ACP `McpServerStdio` entry as a client; anything else (http, sse, malformed) is None."""
    if entry.get("type", "stdio") != "stdio":
        return None
    name, command, args, env = (
        entry.get("name"),
        entry.get("command"),
        entry.get("args"),
        entry.get("env"),
    )
    if not isinstance(name, str) or not isinstance(command, str):
        return None
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return None
    pairs: dict[str, str] = {}
    for item in env if isinstance(env, list) else []:
        if not isinstance(item, dict):
            return None
        key, value = item.get("name"), item.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        pairs[key] = value
    if env is not None and not isinstance(env, list):
        return None
    return McpClient(name, command, args, pairs)
