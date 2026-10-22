"""A scripted ACP agent, run as a real subprocess so the stdio path is tested.

The prompt text picks the behaviour:

- ``/model X -s``  switch model (as OpenClaw's bridge does); reply "Model set to X"
- ``hello``        a thought, two text chunks, a usage update
- ``tool``         an `execute` call that asks permission, then runs or not
- ``environ``      reply ``{"env": sorted(os.environ), "argv": sys.argv}``
- ``whoami``       reply with the session id, model, cwd, options and how it was opened
- ``slow``         stream nothing until `session/cancel`, then stop `cancelled`
- ``crash``        exit mid-turn
- ``fs``           ask the client for `fs/read_text_file`, report the error code
- ``title``        `session_info_update`: a title, a null one, the same title again
- ``selfmode``     the agent switches its own mode to `code`
- ``plan``         a plan, the same plan again, then the plan moved on
- ``usage``        a `usage_update` with a context size and a cost
- ``rounds``       three `usage_update`s (used 100, 200, 300), then a turn usage summing them
- ``sumonly``      a turn usage (input 600) and no `usage_update`
- ``edit``         edit `notes.txt` (whole-file diff, shown only before it is written)
- ``fragment``     edit `code.py` with a fragment diff, reported only once written
- ``create``       create `new.txt`
- ``badedit``      a diff on `notes.txt` whose call fails, writing nothing
- ``askedit``      an edit to `notes.txt` that asks permission first, with its diff
- ``mcp tools``    list the tools the ``ahp_client`` MCP server (the relay the host
                   gave this session) offers, and how many ``tools/list_changed``
                   it has sent: ``{"tools": [...], "changed": N}``
- ``mcp call N J`` call the relay's tool ``N`` with the JSON arguments ``J``, titled
                   ``ahp_client_N``, asking permission first (all of
                   PERMISSION_OPTIONS); reply ``{"chose": id, "result": ...}``
- ``mcp callalways N J``  the same, offering only *always* and *no*
- ``mcp disguise K J``  ask permission for a call titled ``ahp_client_ping`` with kind
                   ``K`` (``none`` for no kind) and raw input ``J``, as a model
                   titling a local call after a relayed tool would; never calls
                   the relay; reply ``{"chose": id}``
- ``permission J``  J is a toolCall object (the rest of the first line): ask permission for
                   it with all of PERMISSION_OPTIONS; reply ``chose <optionId>``
- ``caps``         reply the ``clientCapabilities`` the client sent at initialize, as JSON
- ``clients J``    J is a JSON array of ``{method, params}``: send each to the client as a
                   request (with this session's id; a string ``"$terminal"`` in params
                   becomes the last reply's ``terminalId``); reply a JSON array of each
                   ``result``, or ``{"error": ...}``
- ``structured J``  J is a JSON array of argument objects. List the relay's tools; if
                   ``StructuredOutput`` is not among them, reply "no StructuredOutput
                   tool". Otherwise call it once per object (a call titled
                   ``ahp_client_StructuredOutput``, id ``so_N``, asking permission
                   first), stopping at the first result that is not an error; reply
                   "submitted N" for the N calls made

Each session keeps its history (the prompts it was sent, ``whoami`` aside),
which ``whoami`` reports, so tests can tell sessions -- and forks -- apart.

Environment: ``FAKE_ACP_NATIVE_MODELS=1`` reports ACP session models and
accepts `session/set_model`; ``FAKE_ACP_MODEL_OPTION=1`` offers the model as a
config option of category `model`; ``FAKE_ACP_CONFIG_OPTIONS=1`` offers
`mode` (select), `model` (select, category `model`) and `yolo` (boolean)
config options, refusing `mode = forbidden`; ``FAKE_ACP_MODES=1`` offers
legacy session modes; ``FAKE_ACP_COMMANDS=1`` sends slash commands right
behind `session/new`; ``FAKE_ACP_MCP_HTTP=1`` advertises http MCP servers;
``FAKE_ACP_FORK=1`` and ``FAKE_ACP_CLOSE=1`` offer `session/fork` and
`session/close`; ``FAKE_ACP_PROMPT_CAPS=1`` takes image, audio and embedded
context in prompts; ``FAKE_ACP_STORE`` names a file that keeps session
histories across processes (as an agent that stores its sessions does);
``FAKE_ACP_LOG`` names a file each received method is appended to.

The scenario is the prompt's first text block; the rest is context. The ``mcp``
scenarios match on its first line only, so instructions a host appends to a
prompt do not break them.

Every session's ``mcpServers`` (from session/new, resume, load and fork) are
kept, and the ``mcp`` scenarios start the ``ahp_client`` server from them as
a real subprocess and speak MCP's newline-delimited JSON-RPC to it.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
state: dict[str, Any] = {
    "model": "agent-default",
    "opened": None,
    "cwd": None,
    "options": {},
    "mode": "ask",
    "yolo": False,
    "next_id": 1000,
    "histories": {},
    "mcp": {},
    "mcp_calls": 0,
    "clients": {},
    "client_caps": None,
}
OPTIONS = os.environ.get("FAKE_ACP_CONFIG_OPTIONS") == "1"
MODES = os.environ.get("FAKE_ACP_MODES") == "1"
if OPTIONS:
    state["model"] = "a"


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def update(session_id: str, body: dict[str, Any]) -> None:
    send({"method": "session/update", "params": {"sessionId": session_id, "update": body}})


def text(session_id: str, value: str, kind: str = "agent_message_chunk") -> None:
    update(session_id, {"sessionUpdate": kind, "content": {"type": "text", "text": value}})


def config_options() -> list[dict[str, Any]]:
    """The complete option set, as every ACP answer about options must be."""
    if os.environ.get("FAKE_ACP_MODEL_OPTION") == "1":
        return [
            {
                "id": "model",
                "name": "Model",
                "category": "model",
                "type": "select",
                "currentValue": state["model"],
                "options": [],
            }
        ]
    if not OPTIONS:
        return []
    models = [{"value": "a", "name": "A"}, {"value": "b", "name": "B"}]
    if all(m["value"] != state["model"] for m in models):
        models.append({"value": state["model"], "name": state["model"]})
    return [
        {
            "id": "mode",
            "name": "Mode",
            "category": "mode",
            "type": "select",
            "currentValue": state["mode"],
            "options": [
                {
                    "group": "everyday",
                    "name": "Everyday",
                    "options": [
                        {"value": "ask", "name": "Ask", "description": "Ask before edits"},
                        {"value": "code", "name": "Code"},
                    ],
                },
                {"group": "other", "name": "Other", "options": [{"value": "plan", "name": "Plan"}]},
            ],
        },
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": state["model"],
            "options": models,
        },
        {"id": "yolo", "name": "Auto-approve", "type": "boolean", "currentValue": state["yolo"]},
    ]


def modes() -> dict[str, Any]:
    return {
        "currentModeId": state["mode"],
        "availableModes": [
            {"id": "ask", "name": "Ask"},
            {"id": "code", "name": "Code", "description": "Writes code"},
        ],
    }


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
        if message.get("method"):
            log(message["method"], message.get("params"))
        if predicate(message):
            return message


def ask(method: str, params: dict[str, Any]) -> dict[str, Any]:
    state["next_id"] += 1
    request_id = state["next_id"]
    send({"id": request_id, "method": method, "params": params})
    return wait_for(lambda m: m.get("id") == request_id and "method" not in m)


def read_exact(path: Path) -> str:
    """The file as the bytes on disk say, with no newline translation."""
    return path.read_bytes().decode("utf-8")


def write_exact(path: Path, content: str) -> None:
    """Write ``content`` byte for byte: text mode would turn each LF into CRLF on Windows."""
    path.write_bytes(content.encode("utf-8"))


def edit_call(session_id: str, call_id: str, diff: dict[str, Any], status: str) -> None:
    update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": call_id,
            "title": f"Edit {Path(diff['path']).name}",
            "kind": "edit",
            "status": status,
            "locations": [{"path": diff["path"]}],
            "content": [{"type": "diff", **diff}],
        },
    )


def finish(session_id: str, call_id: str, status: str = "completed") -> None:
    update(
        session_id, {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": status}
    )


#: The relay's server name; must match ahp_host_acp.client_tools.SERVER.
RELAY_NAME = "ahp_client"


class McpClient:
    """A stdio MCP client for one of the servers a session was given."""

    def __init__(self, server: dict[str, Any]) -> None:
        env = {**os.environ, **{e["name"]: e["value"] for e in server.get("env", [])}}
        self.process = subprocess.Popen(
            [server["command"], *server.get("args", [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
        )
        atexit.register(self.process.terminate)
        self.changed = 0
        self.next_id = 0
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "fake-agent", "version": "0"},
            },
        )
        self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def write(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        self.write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self.process.stdout is not None
        while line := self.process.stdout.readline():
            message = json.loads(line)
            if message.get("method") == "notifications/tools/list_changed":
                self.changed += 1
            elif message.get("id") == request_id:
                result: dict[str, Any] = message.get("result", message.get("error", {}))
                return result
        sys.exit(0)


def mcp_client(session_id: str) -> McpClient:
    """The session's relay client, started on first use."""
    clients: dict[str, McpClient] = state["clients"]
    if session_id not in clients:
        servers = state["mcp"].get(session_id, [])
        clients[session_id] = McpClient(next(s for s in servers if s["name"] == RELAY_NAME))
    return clients[session_id]


def mcp_scenario(session_id: str, line: str) -> dict[str, Any] | None:
    """The ``mcp ...`` scenarios; None if *line* is not one."""
    parts = line.split(None, 3)
    if parts[:1] != ["mcp"]:
        return None
    if parts[:2] == ["mcp", "tools"]:
        client = mcp_client(session_id)
        names = [t["name"] for t in client.request("tools/list", {}).get("tools", [])]
        text(session_id, json.dumps({"tools": sorted(names), "changed": client.changed}))
        return {"stopReason": "end_turn"}
    if len(parts) >= 3 and parts[1] == "disguise":
        state["mcp_calls"] += 1
        call: dict[str, Any] = {
            "toolCallId": f"mcp_{state['mcp_calls']}",
            "title": f"{RELAY_NAME}_ping",
            "status": "pending",
            "rawInput": json.loads(parts[3]) if len(parts) > 3 else {},
        }
        if parts[2] != "none":
            call["kind"] = parts[2]
        update(session_id, {"sessionUpdate": "tool_call", **call})
        reply = ask(
            "session/request_permission",
            {"sessionId": session_id, "toolCall": call, "options": PERMISSION_OPTIONS},
        )
        finish(session_id, call["toolCallId"], "failed")
        text(session_id, json.dumps({"chose": chosen(reply)}))
        return {"stopReason": "end_turn"}
    if len(parts) < 3 or parts[1] not in ("call", "callalways"):
        return None
    name = parts[2]
    args = json.loads(parts[3]) if len(parts) > 3 else {}
    state["mcp_calls"] += 1
    call_id = f"mcp_{state['mcp_calls']}"
    call = {
        "toolCallId": call_id,
        "title": f"{RELAY_NAME}_{name}",
        "kind": "other",
        "status": "pending",
        "rawInput": args,
    }
    update(session_id, {"sessionUpdate": "tool_call", **call})
    options = (
        PERMISSION_OPTIONS if parts[1] == "call" else [PERMISSION_OPTIONS[0], PERMISSION_OPTIONS[2]]
    )
    reply = ask(
        "session/request_permission",
        {"sessionId": session_id, "toolCall": call, "options": options},
    )
    picked = chosen(reply)
    if picked in ("once", "always"):
        result = mcp_client(session_id).request("tools/call", {"name": name, "arguments": args})
        finish(session_id, call_id)
        text(session_id, json.dumps({"chose": picked, "result": result}))
    else:
        finish(session_id, call_id, "failed")
        text(session_id, json.dumps({"chose": picked}))
    return {"stopReason": "end_turn"}


def structured_scenario(session_id: str, line: str) -> dict[str, Any] | None:
    """The ``structured <json array>`` scenario; None if *line* is not one."""
    head, _, rest = line.partition(" ")
    if head != "structured":
        return None
    client = mcp_client(session_id)
    names = [t["name"] for t in client.request("tools/list", {}).get("tools", [])]
    if "StructuredOutput" not in names:
        text(session_id, "no StructuredOutput tool")
        return {"stopReason": "end_turn"}
    count = 0
    for args in json.loads(rest):
        count += 1
        call_id = f"so_{count}"
        call = {
            "toolCallId": call_id,
            "title": f"{RELAY_NAME}_StructuredOutput",
            "kind": "other",
            "status": "pending",
            "rawInput": args,
        }
        update(session_id, {"sessionUpdate": "tool_call", **call})
        ask(
            "session/request_permission",
            {"sessionId": session_id, "toolCall": call, "options": PERMISSION_OPTIONS},
        )
        result = client.request("tools/call", {"name": "StructuredOutput", "arguments": args})
        finish(session_id, call_id)
        if not result.get("isError"):
            break
    text(session_id, f"submitted {count}")
    return {"stopReason": "end_turn"}


PERMISSION_OPTIONS = [
    {"optionId": "always", "name": "Always", "kind": "allow_always"},
    {"optionId": "once", "name": "Once", "kind": "allow_once"},
    {"optionId": "no", "name": "No", "kind": "reject_once"},
    {"optionId": "never", "name": "", "kind": "reject_always"},
    {"optionId": "odd", "name": "Odd", "kind": "allow_sometimes"},
]


def chosen(reply: dict[str, Any]) -> Any:
    outcome = reply.get("result", {}).get("outcome", {})
    return outcome.get("optionId", outcome.get("outcome"))


def store(load: bool = False) -> None:
    """Share histories through `FAKE_ACP_STORE`, if there is one."""
    path = os.environ.get("FAKE_ACP_STORE")
    if not path:
        return
    if load:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for key, value in json.load(f).items():
                    state["histories"].setdefault(key, value)
        return
    kept: dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            kept = json.load(f)
    kept.update(state["histories"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(kept, f)


def client_requests(session_id: str, items: list[dict[str, Any]]) -> list[Any]:
    """Send each ``{method, params}`` to the client; collect results (or errors)."""
    replies: list[Any] = []
    terminal: Any = None
    for item in items:
        params = {
            key: terminal if value == "$terminal" else value
            for key, value in item.get("params", {}).items()
        }
        reply = ask(item["method"], {**params, "sessionId": session_id})
        result = reply.get("result")
        if isinstance(result, dict) and "terminalId" in result:
            terminal = result["terminalId"]
        replies.append(result if "error" not in reply else {"error": reply["error"]})
    return replies


def prompt(session_id: str, words: str) -> dict[str, Any]:
    if words != "whoami":
        state["histories"].setdefault(session_id, []).append(words)
        store()
    cwd = Path(state["cwd"] or ".")
    if words.startswith("permission "):
        asked = json.loads(words.split("\n", 1)[0][len("permission ") :])
        update(session_id, {"sessionUpdate": "tool_call", **asked})
        reply = ask(
            "session/request_permission",
            {"sessionId": session_id, "toolCall": asked, "options": PERMISSION_OPTIONS},
        )
        text(session_id, f"chose {chosen(reply)}")
        return {"stopReason": "end_turn"}
    if (answer := mcp_scenario(session_id, words.split("\n", 1)[0])) is not None:
        return answer
    if (answer := structured_scenario(session_id, words.split("\n", 1)[0])) is not None:
        return answer
    if words.startswith("/model "):
        state["model"] = words.split()[1]
        text(session_id, f"Model set to {state['model']}")
        return {"stopReason": "end_turn"}
    if words == "caps":
        text(session_id, json.dumps(state["client_caps"]))
        return {"stopReason": "end_turn"}
    if words.startswith("clients "):
        text(session_id, json.dumps(client_requests(session_id, json.loads(words[8:]))))
        return {"stopReason": "end_turn"}
    if words == "environ":
        text(session_id, json.dumps({"env": sorted(os.environ), "argv": sys.argv}))
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
            {"sessionId": session_id, "toolCall": call, "options": PERMISSION_OPTIONS},
        )
        picked = chosen(reply)
        if picked in ("once", "always"):
            update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call_1",
                    "status": "in_progress",
                    "content": [{"type": "content", "content": {"type": "text", "text": "hi"}}],
                },
            )
            finish(session_id, "call_1")
        else:
            finish(session_id, "call_1", "failed")
        text(session_id, f"chose {picked}")
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
                    "mode": state["mode"],
                    "yolo": state["yolo"],
                    "history": state["histories"].get(session_id, []),
                }
            ),
        )
        return {"stopReason": "end_turn"}
    if words == "slow":
        wait_for(
            lambda m: (
                m.get("method") == "session/cancel"
                and m.get("params", {}).get("sessionId") == session_id
            )
        )
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
    if words == "title":
        for title in ("Fake title", None, "Fake title"):
            update(session_id, {"sessionUpdate": "session_info_update", "title": title})
        text(session_id, "titled")
        return {"stopReason": "end_turn"}
    if words == "selfmode":
        state["mode"] = "code"
        if OPTIONS:
            update(
                session_id,
                {"sessionUpdate": "config_option_update", "configOptions": config_options()},
            )
        if MODES:
            update(session_id, {"sessionUpdate": "current_mode_update", "currentModeId": "code"})
        text(session_id, "switched")
        return {"stopReason": "end_turn"}
    if words == "plan":
        first = [
            {"content": "Read the code", "priority": "high", "status": "pending"},
            {"content": "Fix the bug", "priority": "medium", "status": "pending"},
        ]
        later = [
            {"content": "Read the code", "priority": "high", "status": "completed"},
            {"content": "Fix the bug", "priority": "medium", "status": "in_progress"},
        ]
        for entries in (first, first, later):
            update(session_id, {"sessionUpdate": "plan", "entries": entries})
        text(session_id, "planned")
        return {"stopReason": "end_turn"}
    if words == "usage":
        update(
            session_id,
            {
                "sessionUpdate": "usage_update",
                "used": 10,
                "size": 200000,
                "cost": {"amount": 0.25, "currency": "USD"},
            },
        )
        return {"stopReason": "end_turn"}
    if words == "rounds":
        for used in (100, 200, 300):
            update(
                session_id,
                {
                    "sessionUpdate": "usage_update",
                    "used": used,
                    "size": 200000,
                    "cost": {"amount": 0.5, "currency": "USD"},
                },
            )
        return {
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 600,
                "outputTokens": 40,
                "cachedReadTokens": 7,
                "totalTokens": 640,
            },
        }
    if words == "sumonly":
        return {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 600, "outputTokens": 40, "totalTokens": 640},
        }
    if words == "edit":
        path = cwd / "notes.txt"
        old = read_exact(path)
        new = old + "more\n"
        diff = {"path": str(path), "oldText": old, "newText": new}
        edit_call(session_id, "edit_1", diff, "pending")
        write_exact(path, new)
        # The result replaces the diff the call showed while it ran.
        update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "edit_1",
                "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": "Saved."}}],
            },
        )
        return {"stopReason": "end_turn"}
    if words == "fragment":
        path = cwd / "code.py"
        write_exact(path, read_exact(path).replace("b = 2", "b = 3"))
        diff = {"path": str(path), "oldText": "b = 2", "newText": "b = 3"}
        edit_call(session_id, "edit_2", diff, "completed")
        return {"stopReason": "end_turn"}
    if words == "create":
        path = cwd / "new.txt"
        write_exact(path, "hello\n")
        edit_call(session_id, "edit_3", {"path": str(path), "newText": "hello\n"}, "completed")
        return {"stopReason": "end_turn"}
    if words == "askedit":
        path = cwd / "notes.txt"
        old = read_exact(path)
        diff = {"path": str(path), "oldText": old, "newText": old + "asked\n"}
        edit_asked: dict[str, Any] = {
            "toolCallId": "edit_5",
            "title": "Edit notes.txt",
            "kind": "edit",
            "status": "pending",
            "content": [{"type": "diff", **diff}],
        }
        update(session_id, {"sessionUpdate": "tool_call", **edit_asked})
        reply = ask(
            "session/request_permission",
            {"sessionId": session_id, "toolCall": edit_asked, "options": PERMISSION_OPTIONS[:3]},
        )
        if chosen(reply) in ("once", "always"):
            write_exact(path, diff["newText"])
            finish(session_id, "edit_5")
        else:
            finish(session_id, "edit_5", "failed")
        return {"stopReason": "end_turn"}
    if words == "badedit":
        path = cwd / "notes.txt"
        diff = {"path": str(path), "oldText": read_exact(path), "newText": "gone\n"}
        edit_call(session_id, "edit_4", diff, "pending")
        finish(session_id, "edit_4", "failed")
        return {"stopReason": "end_turn"}
    text(session_id, f"echo: {words}")
    return {"stopReason": "end_turn"}


def set_config_option(params: dict[str, Any]) -> dict[str, Any] | None:
    """The new full option set, or None to refuse."""
    config_id, value = params["configId"], params.get("value")
    if config_id == "bogus":
        return None
    if config_id == "model":
        state["model"] = value
    elif OPTIONS and config_id == "mode":
        if value not in ("ask", "code", "plan"):
            return None
        state["mode"] = value
    elif OPTIONS and config_id == "yolo":
        if params.get("type") != "boolean" or not isinstance(value, bool):
            return None
        state["yolo"] = value
    else:
        state["options"][config_id] = value
    return {"configOptions": config_options()}


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
        after: list[dict[str, Any]] = []
        if method == "initialize":
            state["client_caps"] = params.get("clientCapabilities")
            capabilities: dict[str, Any] = {
                "loadSession": True,
                "sessionCapabilities": {"resume": {}},
            }
            if os.environ.get("FAKE_ACP_MCP_HTTP") == "1":
                capabilities["mcpCapabilities"] = {"http": True}
            if os.environ.get("FAKE_ACP_PROMPT_CAPS") == "1":
                capabilities["promptCapabilities"] = {
                    "image": True,
                    "audio": True,
                    "embeddedContext": True,
                }
            for flag, name in (("FAKE_ACP_FORK", "fork"), ("FAKE_ACP_CLOSE", "close")):
                if os.environ.get(flag) == "1":
                    capabilities["sessionCapabilities"][name] = {}
            result = {"protocolVersion": 1, "agentCapabilities": capabilities, "authMethods": []}
        elif method in ("session/new", "session/resume", "session/load", "session/fork"):
            if method == "session/fork" and os.environ.get("FAKE_ACP_FORK") != "1":
                send({"id": request_id, "error": {"code": -32601, "message": "no fork"}})
                continue
            state["cwd"] = params.get("cwd")
            state["opened"] = method
            if method in ("session/new", "session/fork"):
                session_id = str(uuid.uuid4())
                source = state["histories"].get(params.get("sessionId"), [])
                state["histories"][session_id] = list(source) if method == "session/fork" else []
                store()
            else:
                session_id = params["sessionId"]
                store(load=True)
                state["histories"].setdefault(session_id, [])
            state["session"] = session_id
            state["mcp"][session_id] = params.get("mcpServers", [])
            result = {"sessionId": session_id} if method in ("session/new", "session/fork") else {}
            if (config_options() and method == "session/new") or OPTIONS:
                result["configOptions"] = config_options()
            if MODES:
                result["modes"] = modes()
            if native:
                result["models"] = {
                    "currentModelId": state["model"],
                    "availableModels": [
                        {"modelId": "a", "name": "A"},
                        {"modelId": "b", "name": "B"},
                    ],
                }
            if os.environ.get("FAKE_ACP_COMMANDS") == "1":
                after.append(
                    {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": [
                            {
                                "name": "web",
                                "description": "Search the web",
                                "input": {"hint": "query"},
                            },
                            {"name": "help", "description": "Show help"},
                        ],
                    }
                )
        elif method == "session/set_config_option":
            answer = set_config_option(params)
            if answer is None:
                send({"id": request_id, "error": {"code": -32602, "message": "no such option"}})
                continue
            result = answer
        elif method == "session/set_mode" and MODES:
            if params["modeId"] not in ("ask", "code"):
                send({"id": request_id, "error": {"code": -32602, "message": "no such mode"}})
                continue
            state["mode"] = params["modeId"]
            result = {}
        elif method == "session/close" and os.environ.get("FAKE_ACP_CLOSE") == "1":
            state["histories"].pop(params["sessionId"], None)
            result = {}
        elif method == "session/set_model" and native:
            state["model"] = params["modelId"]
            result = {}
        elif method == "session/prompt":
            words = next((b["text"] for b in params["prompt"] if b.get("type") == "text"), "")
            result = prompt(params["sessionId"], words.strip())
        else:
            send({"id": request_id, "error": {"code": -32601, "message": f"no {method}"}})
            continue
        lines = [{"jsonrpc": "2.0", "id": request_id, "result": result}]
        # Right behind the answer, in the same write: the client may not have
        # taken in the session id yet when these arrive.
        lines += [
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": state["session"], "update": body},
            }
            for body in after
        ]
        sys.stdout.write("".join(json.dumps(line) + "\n" for line in lines))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
