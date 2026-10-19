"""A stdio MCP server that relays an ACP session's client tools to the host.

ACP's `mcpServers` name servers the *agent* connects to, and "all Agents MUST
support" the `stdio` transport, so this is the one way to give any ACP agent
tools that run elsewhere (`client_tools.py` says which). The agent launches
this module with `sys.executable -m ahp_host_acp.relay --port N` and speaks
MCP's newline-delimited JSON-RPC to it; each `tools/list` and `tools/call` is
forwarded over a socket (loopback unless `--host` says otherwise) to the host
(`client_tools.ToolBridge`), which answers with the tool list or the result.

Any local process can reach a loopback port, so the first line this sends is a
per-session random token, and the host answers nothing to a wrong one. The
token travels only in the environment of the server entry the agent was
given (`TOKEN_ENV`), never on a command line, where other users' `ps` would
show it.

It runs inside the agent's process tree, so it is standard library only and
imports nothing else from this package.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import threading
from collections.abc import Sequence
from typing import Any, Final

#: The environment variable the host passes the session's token in.
TOKEN_ENV: Final = "AHP_ACP_RELAY_TOKEN"
#: MCP protocol versions this answers as itself; others get the default.
MCP_VERSIONS: Final = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_MCP_VERSION: Final = "2025-06-18"

_stdout_lock = threading.Lock()
_socket_lock = threading.Lock()
#: Requests waiting for the host: internal id -> (set when answered, the answer).
_pending: dict[int, tuple[threading.Event, list[dict[str, Any]]]] = {}
_pending_lock = threading.Lock()
_ids = iter(range(1, 1 << 62))


def _line(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=True) + "\n"


def _write_stdout(message: dict[str, Any]) -> None:
    with _stdout_lock:
        sys.stdout.write(_line(message))
        sys.stdout.flush()


def _reply(request_id: Any, result: Any) -> None:
    _write_stdout({"jsonrpc": "2.0", "id": request_id, "result": result})


def _fail(request_id: Any, code: int, message: str) -> None:
    _write_stdout({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _send(sock: socket.socket, message: dict[str, Any]) -> None:
    with _socket_lock:
        sock.sendall(_line(message).encode("ascii"))


def _read_bridge(reader: Any) -> None:
    """Deliver the host's answers, and tell the agent its tools changed."""
    for raw in reader:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if "id" in message:
            with _pending_lock:
                waiting = _pending.pop(message["id"], None)
            if waiting is not None:
                waiting[1].append(message)
                waiting[0].set()
        elif message.get("method") == "tools/list_changed":
            _write_stdout({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    sys.stderr.write("the host closed the connection\n")
    sys.stderr.flush()
    os._exit(1)


def _forward(sock: socket.socket, request_id: Any, method: str, params: Any) -> None:
    event = threading.Event()
    slot: list[dict[str, Any]] = []
    with _pending_lock:
        internal = next(_ids)
        _pending[internal] = (event, slot)
    try:
        _send(sock, {"id": internal, "method": method, "params": params})
    except OSError as exc:
        _fail(request_id, -32603, f"the host is unreachable: {exc}")
        return
    event.wait()
    answer = slot[0]
    if "error" in answer:
        error = answer["error"]
        message = error.get("message") if isinstance(error, dict) else None
        _fail(request_id, -32603, message if isinstance(message, str) else "the host failed")
    else:
        _reply(request_id, answer.get("result"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ahp_host_acp.relay")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    # MCP's stdio transport is UTF-8 with "\n" line ends. Python's defaults are
    # the locale code page (cp1252 on Windows) and "\r\n" there, which garble
    # non-ASCII arguments and can kill the loop on a byte cp1252 lacks.
    for stream in (sys.stdin, sys.stdout):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", newline="\n")
    token = os.environ.get(TOKEN_ENV)
    if not token:
        sys.stderr.write(f"{TOKEN_ENV} is not set\n")
        return 2
    sock = socket.create_connection((args.host, args.port), timeout=10)
    sock.settimeout(None)
    reader = sock.makefile("r", encoding="utf-8", newline="\n")
    _send(sock, {"token": token})
    try:
        greeting = json.loads(reader.readline() or "null")
    except ValueError:
        greeting = None
    if greeting != {"ok": True}:
        sys.stderr.write("the host refused the relay's token\n")
        return 1
    threading.Thread(target=_read_bridge, args=(reader,), daemon=True).start()

    for raw in sys.stdin:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue
        request_id = message["id"]
        method = message.get("method")
        params = message.get("params")
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            _reply(
                request_id,
                {
                    "protocolVersion": asked if asked in MCP_VERSIONS else DEFAULT_MCP_VERSION,
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "ahp-client-tools", "version": "0.1.0"},
                },
            )
        elif method == "ping":
            _reply(request_id, {})
        elif method in ("tools/list", "tools/call"):
            threading.Thread(
                target=_forward, args=(sock, request_id, method, params), daemon=True
            ).start()
        else:
            _fail(request_id, -32601, f"no {method}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
