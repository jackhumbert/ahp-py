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


def _ask_permission(session_id: Any, n: int, command: str) -> str:
    """Send `session/request_permission` for a shell *command*; the option id the host picked."""
    request_id = f"perm-{n}"
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": f"call-{n}",
                    "title": command,
                    "kind": "execute",
                    "rawInput": {"command": command},
                },
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
    )
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        message = json.loads(line.decode("utf-8"))
        if message.get("id") == request_id and "method" not in message:
            outcome = (message.get("result") or {}).get("outcome") or {}
            return str(outcome.get("optionId") or outcome.get("outcome"))
    return "eof"


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
            text = _prompt_text(params).split("\n\n")[-1]
            if text.startswith("perm: "):
                # Ask to run each ';'-separated command; the reply names the picked options.
                picked = [
                    _ask_permission(params.get("sessionId"), n, command.strip())
                    for n, command in enumerate(text.removeprefix("perm: ").split(";"))
                ]
                text = " ".join(picked)
            reply = "acp: " + text
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
