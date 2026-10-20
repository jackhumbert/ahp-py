"""A blocking client for an OpenAI-compatible streaming `/v1/chat/completions` endpoint.

It uses `http.client`, not `urllib`, so another thread can shut the in-flight socket down
(`ChatClient.abort`) to cancel a turn that is blocked reading. Standard library only.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

#: Most of an error response read, in bytes.
MAX_ERROR_BODY = 65536
#: Longest detail quoted from an error response, in characters.
MAX_DETAIL = 300
#: Longest single event line read, in bytes.
MAX_LINE = 1048576


class EndpointError(Exception):
    """The endpoint refused, failed or could not be reached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class Cancelled(Exception):  # noqa: N818
    """The turn was cancelled while the request was in flight."""


@dataclass
class ToolCallDraft:
    id: str
    name: str
    #: The raw JSON text of the arguments, as streamed.
    arguments: str


@dataclass
class Completion:
    text: str
    reasoning: str
    tool_calls: list[ToolCallDraft] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


def request_body(
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = list(tools)
    return body


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage(raw: Mapping[str, Any]) -> dict[str, int]:
    prompt = _int(raw.get("prompt_tokens"))
    completion = _int(raw.get("completion_tokens"))
    details = raw.get("prompt_tokens_details")
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    return {
        "prompt": prompt,
        "completion": completion,
        "cached": cached,
        "total": _int(raw.get("total_tokens")) or prompt + completion,
    }


def _accumulate(drafts: dict[int, dict[str, str]], calls: Sequence[Any]) -> None:
    for item in calls:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        draft = drafts.setdefault(
            index if isinstance(index, int) else 0, {"id": "", "name": "", "arguments": ""}
        )
        call_id = item.get("id")
        if isinstance(call_id, str) and call_id and not draft["id"]:
            draft["id"] = call_id
        function = item.get("function")
        if isinstance(function, dict):
            for key in ("name", "arguments"):
                part = function.get(key)
                if isinstance(part, str):
                    draft[key] += part


class ChatClient:
    def __init__(self, base_url: str, model: str, api_key: str | None, timeout: float) -> None:
        self._parsed = urlsplit(base_url)
        self._model = model
        self._key = api_key
        self._timeout = timeout
        path = self._parsed.path.rstrip("/") + "/chat/completions"
        if self._parsed.query:
            path += "?" + self._parsed.query
        self._path = path
        self._conn: http.client.HTTPConnection | None = None
        # Kept apart from the connection: for a close-delimited response `getresponse` hands the
        # socket to the response and sets `conn.sock = None`, so abort could not reach it there.
        self._sock: socket.socket | None = None
        #: Set by `abort`, cleared when a request starts: an abort before the socket exists is
        #: acted on as soon as it does.
        self._aborted = False
        self._lock = threading.Lock()

    def _connect(self) -> http.client.HTTPConnection:
        host = self._parsed.hostname or ""
        port = self._parsed.port
        if self._parsed.scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=self._timeout)
        return http.client.HTTPConnection(host, port, timeout=self._timeout)

    def abort(self) -> None:
        """Stop the request in flight (any thread may call). A blocked connect, send or read then
        fails; an abort during connect, before the socket exists, closes it once it does."""
        with self._lock:
            self._aborted = True
            sock = self._sock
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    def _refusal(self, resp: http.client.HTTPResponse) -> EndpointError:
        raw = resp.read(MAX_ERROR_BODY).decode("utf-8", "replace")
        detail = raw.strip()
        with contextlib.suppress(ValueError):
            parsed = json.loads(raw)
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                detail = error["message"]
        if len(detail) > MAX_DETAIL:
            detail = detail[: MAX_DETAIL - 1] + "…"
        message = f"the endpoint answered HTTP {resp.status}: {detail}"
        if resp.status == 429:
            message = "rate limited: " + message
        return EndpointError(redact(message, self._key), resp.status)

    def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        on_text: Callable[[str], None],
        on_reasoning: Callable[[str], None],
        cancelled: threading.Event,
    ) -> Completion:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        payload = json.dumps(request_body(self._model, messages, tools)).encode("utf-8")
        text: list[str] = []
        reasoning: list[str] = []
        drafts: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage: dict[str, int] | None = None
        conn = self._connect()
        with self._lock:
            self._conn = conn
            self._aborted = False
        if cancelled.is_set():
            with self._lock:
                self._conn = None
            raise Cancelled
        try:
            try:
                conn.connect()
                with self._lock:
                    self._sock = conn.sock
                    stop = self._aborted or cancelled.is_set()
                if stop:
                    raise Cancelled
                conn.request("POST", self._path, body=payload, headers=headers)
                resp = conn.getresponse()
                if resp.status != 200:
                    raise self._refusal(resp)
                while True:
                    line = resp.readline(MAX_LINE)
                    if not line:
                        break
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].decode("utf-8", "replace").strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    error = event.get("error")
                    if isinstance(error, dict):
                        message = error.get("message") or str(error)
                        raise EndpointError(redact(str(message), self._key), None)
                    raw_usage = event.get("usage")
                    if isinstance(raw_usage, dict):
                        usage = _usage(raw_usage)
                    choices = event.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta")
                    if isinstance(delta, dict):
                        content = delta.get("content")
                        if isinstance(content, str):
                            text.append(content)
                            on_text(content)
                        thought = delta.get("reasoning_content")
                        if not isinstance(thought, str):
                            thought = delta.get("reasoning")
                        if isinstance(thought, str):
                            reasoning.append(thought)
                            on_reasoning(thought)
                        calls = delta.get("tool_calls")
                        if isinstance(calls, list):
                            _accumulate(drafts, calls)
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        finish = str(reason)
            except (OSError, http.client.HTTPException) as exc:
                if cancelled.is_set() or self._aborted:
                    raise Cancelled from exc
                raise EndpointError(
                    redact(f"the endpoint connection failed: {exc}", self._key), None
                ) from exc
            if cancelled.is_set() or self._aborted:
                raise Cancelled
        finally:
            with self._lock:
                self._conn = None
                self._sock = None
            with contextlib.suppress(OSError):
                conn.close()
        tool_calls = [
            ToolCallDraft(d["id"] or f"call_{index}", d["name"], d["arguments"])
            for index, d in sorted(drafts.items())
        ]
        return Completion("".join(text), "".join(reasoning), tool_calls, finish, usage)
