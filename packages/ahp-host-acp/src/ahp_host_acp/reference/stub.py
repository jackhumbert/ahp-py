"""A scripted chat-completions server for tests and demos (standard library only).

Each POST pops the next `Reply` off the script and answers it as a server-sent-event stream
(or an error status), so the reference agent, `ahp-host-flow`'s tests and the wheel smoke can
run without a real model. Requests are recorded before they are answered.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: How long a `hang` reply keeps the stream open before giving up, in seconds.
HANG_LIMIT = 30.0
#: Pause between keepalive comments of a `hang` reply, in seconds.
HANG_TICK = 0.05


@dataclass(frozen=True)
class Reply:
    deltas: tuple[Mapping[str, Any], ...] = ()
    finish_reason: str | None = "stop"
    usage: Mapping[str, Any] | None = None
    status: int = 200
    body: str = ""
    #: Send the deltas, then keep the stream open until the client disconnects.
    hang: bool = False


def say(
    *texts: str,
    reasoning: Sequence[str] = (),
    usage: Mapping[str, Any] | None = None,
) -> Reply:
    deltas: list[Mapping[str, Any]] = [{"reasoning_content": r} for r in reasoning]
    deltas += [{"content": t} for t in texts]
    return Reply(deltas=tuple(deltas), finish_reason="stop", usage=usage)


def call(name: str, arguments: Mapping[str, Any], *, call_id: str = "call_1") -> Reply:
    text = json.dumps(arguments, separators=(",", ":"))
    half = len(text) // 2
    first = {
        "tool_calls": [
            {
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": text[:half]},
            }
        ]
    }
    second = {"tool_calls": [{"index": 0, "function": {"arguments": text[half:]}}]}
    return Reply(deltas=(first, second), finish_reason="tool_calls")


def refuse(status: int = 429, message: str = "Rate limit exceeded") -> Reply:
    kind = "rate_limit_error" if status == 429 else "error"
    body = json.dumps({"error": {"message": message, "type": kind}})
    return Reply(status=status, body=body)


class StubServer:
    def __init__(self, replies: Iterable[Reply], *, model: str = "stub-model") -> None:
        self._script = list(replies)
        self._model = model
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.paths: list[str] = []
        #: Set when a client went away while a `hang` reply was open.
        self.disconnected = threading.Event()

    def start(self) -> StubServer:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:
                stub._handle(self)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> StubServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def agent_env(self, *, model: str = "stub-model", api_key: str | None = None) -> dict[str, str]:
        env = {"AHP_OPENAI_BASE_URL": self.base_url, "AHP_OPENAI_MODEL": model}
        if api_key:
            env["AHP_OPENAI_API_KEY"] = api_key
        return env

    def _chunk(self, delta: Mapping[str, Any], finish: str | None) -> bytes:
        event = {
            "id": "chatcmpl-stub",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": self._model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(event)}\n\n".encode()

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        chat = handler.path.split("?")[0].endswith("/chat/completions")
        reply: Reply | None = None
        with self._lock:
            self.paths.append(handler.path)
            self.headers.append({k.lower(): v for k, v in handler.headers.items()})
            self.requests.append(body)
            if chat and self._script:
                reply = self._script.pop(0)
        if not chat:
            self._plain(handler, 404, json.dumps({"error": {"message": "not found"}}))
        elif reply is None:
            message = json.dumps({"error": {"message": "stub script exhausted"}})
            self._plain(handler, 500, message)
        elif reply.status != 200:
            self._plain(handler, reply.status, reply.body)
        else:
            self._stream(handler, reply)

    @staticmethod
    def _plain(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
        data = body.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _stream(self, handler: BaseHTTPRequestHandler, reply: Reply) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True

        def write(data: bytes) -> None:
            handler.wfile.write(data)
            handler.wfile.flush()

        try:
            for delta in reply.deltas:
                write(self._chunk(delta, None))
            if reply.hang:
                deadline = time.monotonic() + HANG_LIMIT
                while time.monotonic() < deadline:
                    write(b": keepalive\n\n")
                    time.sleep(HANG_TICK)
                return
            if reply.finish_reason is not None:
                write(self._chunk({}, reply.finish_reason))
            if reply.usage:
                tail = {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": self._model,
                    "choices": [],
                    "usage": dict(reply.usage),
                }
                write(f"data: {json.dumps(tail)}\n\n".encode())
            write(b"data: [DONE]\n\n")
        except OSError:
            if reply.hang:
                self.disconnected.set()
