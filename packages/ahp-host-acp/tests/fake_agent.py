"""A scripted ACP agent, run as a real subprocess so the stdio path is tested.

The prompt text picks the behaviour:

- ``/model X -s``  switch model (as OpenClaw's bridge does); reply "Model set to X"
- ``hello``        a thought, two text chunks, a usage update
- ``tool``         an `execute` call that asks permission, then runs or not
- ``whoami``       reply with the session id, model, cwd and how it was opened
- ``slow``         stream nothing until `session/cancel`, then stop `cancelled`
- ``crash``        exit mid-turn
- ``fs``           ask the client for `fs/read_text_file`, report the error code

Environment: ``FAKE_ACP_NATIVE_MODELS=1`` reports ACP session models and
accepts `session/set_model`; ``FAKE_ACP_LOG`` names a file each received method
is appended to.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import uuid
from typing import Any

inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
state: dict[str, Any] = {
    "model": "agent-default",
    "opened": None,
    "cwd": None,
    "options": {},
    "next_id": 1000,
}


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def update(session_id: str, body: dict[str, Any]) -> None:
    send({"method": "session/update", "params": {"sessionId": session_id, "update": body}})


def text(session_id: str, value: str, kind: str = "agent_message_chunk") -> None:
    update(session_id, {"sessionUpdate": kind, "content": {"type": "text", "text": value}})


def reader() -> None:
    for line in sys.stdin:
        line = line.strip()
        if line:
            inbox.put(json.loads(line))
    inbox.put(None)


def log(method: str, params: Any) -> None:
    path = os.environ.get("FAKE_ACP_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"method": method, "params": params}) + "\n")


def wait_for(predicate: Any) -> dict[str, Any]:
    """Next message matching `predicate`; others are handled as they come."""
    while True:
        message = inbox.get()
        if message is None:
            sys.exit(0)
        if predicate(message):
            return message
        if message.get("method"):
            log(message["method"], message.get("params"))


def ask(method: str, params: dict[str, Any]) -> dict[str, Any]:
    state["next_id"] += 1
    request_id = state["next_id"]
    send({"id": request_id, "method": method, "params": params})
    return wait_for(lambda m: m.get("id") == request_id and "method" not in m)


def prompt(session_id: str, words: str) -> dict[str, Any]:
    if words.startswith("/model "):
        state["model"] = words.split()[1]
        text(session_id, f"Model set to {state['model']}")
        return {"stopReason": "end_turn"}
    if words == "hello":
        text(session_id, "thinking…", "agent_thought_chunk")
        text(session_id, "Hi ")
        text(session_id, "there")
        update(session_id, {"sessionUpdate": "usage_update", "used": 1234, "size": 100000})
        return {"stopReason": "end_turn"}
    if words == "tool":
        call = {
            "toolCallId": "call_1",
            "title": "exec: echo hi",
            "kind": "execute",
            "status": "pending",
            "rawInput": {"command": "echo hi", "title": "Run echo hi"},
        }
        update(session_id, {"sessionUpdate": "tool_call", **call})
        reply = ask(
            "session/request_permission",
            {
                "sessionId": session_id,
                "toolCall": call,
                "options": [
                    {"optionId": "always", "name": "Always", "kind": "allow_always"},
                    {"optionId": "once", "name": "Once", "kind": "allow_once"},
                    {"optionId": "no", "name": "No", "kind": "reject_once"},
                ],
            },
        )
        outcome = reply.get("result", {}).get("outcome", {})
        chosen = outcome.get("optionId", outcome.get("outcome"))
        if chosen == "once":
            update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call_1",
                    "status": "in_progress",
                    "content": [{"type": "content", "content": {"type": "text", "text": "hi"}}],
                },
            )
            update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call_1",
                    "status": "completed",
                },
            )
        else:
            update(
                session_id,
                {"sessionUpdate": "tool_call_update", "toolCallId": "call_1", "status": "failed"},
            )
        text(session_id, f"chose {chosen}")
        return {"stopReason": "end_turn"}
    if words == "whoami":
        text(
            session_id,
            json.dumps(
                {
                    "session": session_id,
                    "model": state["model"],
                    "cwd": state["cwd"],
                    "opened": state["opened"],
                    "options": state["options"],
                }
            ),
        )
        return {"stopReason": "end_turn"}
    if words == "slow":
        wait_for(lambda m: m.get("method") == "session/cancel")
        return {"stopReason": "cancelled"}
    if words == "crash":
        text(session_id, "about to crash")
        sys.stderr.write("fake agent: crashing on purpose\n")
        sys.stderr.flush()
        os._exit(3)
    if words == "fs":
        reply = ask("fs/read_text_file", {"sessionId": session_id, "path": "/etc/passwd"})
        text(session_id, f"fs error {reply.get('error', {}).get('code')}")
        return {"stopReason": "end_turn"}
    if words == "limit":
        return {"stopReason": "max_tokens"}
    text(session_id, f"echo: {words}")
    return {"stopReason": "end_turn"}


def main() -> None:
    threading.Thread(target=reader, daemon=True).start()
    native = os.environ.get("FAKE_ACP_NATIVE_MODELS") == "1"
    print("a banner line that is not JSON", flush=True)
    while True:
        message = inbox.get()
        if message is None:
            return
        method, params, request_id = (
            message.get("method"),
            message.get("params") or {},
            message.get("id"),
        )
        if method is None:
            continue
        log(method, params)
        if request_id is None:
            continue  # a notification outside a turn (a late cancel)
        result: Any
        if method == "initialize":
            result = {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"resume": {}}},
                "authMethods": [],
            }
        elif method in ("session/new", "session/resume", "session/load"):
            state["cwd"] = params.get("cwd")
            state["opened"] = method
            session_id = params.get("sessionId") or str(uuid.uuid4())
            state["session"] = session_id
            result = {"sessionId": session_id} if method == "session/new" else {}
            if native:
                result["models"] = {
                    "currentModelId": state["model"],
                    "availableModels": [
                        {"modelId": "a", "name": "A"},
                        {"modelId": "b", "name": "B"},
                    ],
                }
        elif method == "session/set_config_option":
            if params["configId"] == "bogus":
                send({"id": request_id, "error": {"code": -32602, "message": "no such option"}})
                continue
            state["options"][params["configId"]] = params["value"]
            result = {"configOptions": []}
        elif method == "session/set_model" and native:
            state["model"] = params["modelId"]
            result = {}
        elif method == "session/prompt":
            words = " ".join(b.get("text", "") for b in params["prompt"] if b.get("type") == "text")
            result = prompt(params["sessionId"], words.strip())
        else:
            send({"id": request_id, "error": {"code": -32601, "message": f"no {method}"}})
            continue
        send({"id": request_id, "result": result})


if __name__ == "__main__":
    main()
