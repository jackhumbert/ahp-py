"""A stand-in ACP agent for the flow tests: standard library only, run as a script.

Speaks newline-delimited JSON-RPC on stdin/stdout. A prompt is answered `acp: <text>`, where
the text is the last blank-line-separated paragraph of the prompt, so a preamble the host
prepends never changes the reply.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _send(message: dict[str, Any]) -> None:
    sys.stdout.buffer.write(json.dumps(message).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _reply(request_id: Any, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _prompt_text(params: dict[str, Any]) -> str:
    for block in params.get("prompt", []):
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", ""))
    return ""


def main() -> None:
    sessions = 0
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        message = json.loads(line.decode("utf-8"))
        if "id" not in message:
            continue
        request_id = message["id"]
        method = message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            _reply(
                request_id,
                {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []},
            )
        elif method == "session/new":
            sessions += 1
            _reply(request_id, {"sessionId": f"fake-{sessions}"})
        elif method == "session/prompt":
            reply = "acp: " + _prompt_text(params).split("\n\n")[-1]
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": params.get("sessionId"),
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": reply},
                        },
                    },
                }
            )
            _reply(request_id, {"stopReason": "end_turn"})
        else:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "not supported"},
                }
            )


if __name__ == "__main__":
    main()
