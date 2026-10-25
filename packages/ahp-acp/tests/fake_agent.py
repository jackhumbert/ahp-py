"""A scripted ACP agent, run as a real subprocess.

    python fake_agent.py '<json script>'

The script is one JSON object:

    {
      "responses": {"<method>": <result>},     # sent for each request of that method
      "errors": {"<method>": {"code": ..., "message": ...}},
      "before": {"<method>": [<step>, ...]},   # run before answering that method
      "echo": ["<method>", ...],               # add {"echo": params} to that result
      "ignore_eof": false                      # keep running after stdin closes
    }

Steps: {"send": <message>} writes it to stdout; {"await": [<id>, ...]} waits for
the client's replies to those ids and stores them; {"stderr": "text"};
{"raw": "text"} writes a line verbatim; {"big_line": n} writes n bytes then a
newline; {"big_stderr": n} does the same on stderr; {"stderr_fill": n} writes n
1 KB lines to stderr; {"grandchild": true} starts a sleeping child and reports its pid in an
`_example/pid` notification; {"sleep": seconds}; {"exit": code} ends the process
at once; {"env": true} reports the environment in an `_example/env` notification.
A result containing "$replies" gets the stored replies.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

script: dict[str, Any] = json.loads(sys.argv[1])
replies: dict[str, Any] = {}


def out(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def handle_reply(message: dict[str, Any]) -> None:
    replies[str(message["id"])] = message.get("result", message.get("error"))


def read(once: bool = False) -> dict[str, Any] | None:
    """The next request or notification; replies are stored on the way. With
    `once`, return after one line (None for a reply) so a waiter can re-check."""
    while True:
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        message = json.loads(line)
        if "method" not in message:
            handle_reply(message)
            if once:
                return None
            continue
        return message  # type: ignore[no-any-return]


def run_steps(steps: list[dict[str, Any]]) -> None:
    for step in steps:
        if "send" in step:
            out(step["send"])
        elif "await" in step:
            while not all(str(i) in replies for i in step["await"]):
                read(once=True)
        elif "stderr" in step:
            sys.stderr.write(step["stderr"] + "\n")
            sys.stderr.flush()
        elif "raw" in step:
            sys.stdout.write(step["raw"] + "\n")
            sys.stdout.flush()
        elif "big_line" in step:
            sys.stdout.write("x" * step["big_line"] + "\n")
            sys.stdout.flush()
        elif "big_stderr" in step:
            sys.stderr.write("e" * step["big_stderr"] + "\n")
            sys.stderr.flush()
        elif "stderr_fill" in step:
            for n in range(step["stderr_fill"]):
                sys.stderr.write(f"{n:06d}" + "f" * 1017 + "\n")
            sys.stderr.flush()
        elif "grandchild" in step:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
            )
            out({"method": "_example/pid", "params": {"pid": child.pid}})
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif "exit" in step:
            os._exit(step["exit"])
        elif "env" in step:
            out({"method": "_example/env", "params": {"env": dict(os.environ)}})


def main() -> None:
    if script.get("ignore_eof") or script.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        try:
            message = read()
        except EOFError:
            if script.get("ignore_eof"):
                time.sleep(60)
            return
        assert message is not None
        method = message["method"]
        if "id" not in message:
            continue
        run_steps(script.get("before", {}).get(method, []))
        if method in script.get("errors", {}):
            out({"id": message["id"], "error": script["errors"][method]})
            continue
        result = dict(script.get("responses", {}).get(method, {}))
        if method in script.get("echo", []):
            result["echo"] = message.get("params")
        if result.pop("$replies", False):
            result["replies"] = replies
        out({"id": message["id"], "result": result})


main()
