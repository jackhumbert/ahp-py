"""A tiny MCP server over stdio for tests: newline-delimited JSON-RPC, like the relay."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

ECHO = {
    "name": "echo",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}
FAIL = {"name": "fail", "inputSchema": {"type": "object", "properties": {}}}
#: Listed only with FAKE_MCP_EXTRA=1: `env` answers an environment variable's value (or
#: "<unset>"), `hang` never answers (until the client gives up).
ENV = {
    "name": "env",
    "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
}
HANG = {"name": "hang", "inputSchema": {"type": "object", "properties": {}}}


def reply(request_id: Any, result: Any = None, error: Any = None) -> None:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def list_tools(params: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("FAKE_MCP_PAGED") == "1":
        if params.get("cursor") == "p2":
            return {"tools": [FAIL]}
        return {"tools": [ECHO], "nextCursor": "p2"}
    if os.environ.get("FAKE_MCP_EXTRA") == "1":
        return {"tools": [ECHO, FAIL, ENV, HANG]}
    return {"tools": [ECHO, FAIL]}


def call_tool(request_id: Any, params: dict[str, Any]) -> None:
    name = params.get("name")
    if name == "echo":
        text = (params.get("arguments") or {}).get("text")
        reply(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
    elif name == "env":
        key = str((params.get("arguments") or {}).get("name"))
        text = os.environ.get(key, "<unset>")
        reply(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
    elif name == "hang":
        return
    elif name == "fail":
        reply(request_id, {"content": [{"type": "text", "text": "it failed"}], "isError": True})
    else:
        reply(request_id, error={"code": -32602, "message": "unknown tool"})


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        if "id" not in message:
            continue
        request_id, method = message["id"], message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            reply(
                request_id,
                {
                    "protocolVersion": params.get("protocolVersion"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-tools", "version": "0"},
                },
            )
        elif method == "tools/list":
            reply(request_id, list_tools(params))
        elif method == "tools/call":
            call_tool(request_id, params)
        else:
            reply(request_id, error={"code": -32601, "message": "method not found"})


if __name__ == "__main__":
    main()
