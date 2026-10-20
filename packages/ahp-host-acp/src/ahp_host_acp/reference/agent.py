"""The reference ACP agent: newline-delimited JSON-RPC over stdio, an OpenAI-compatible model.

One `Agent` serves one client. Each request runs on its own daemon thread (a turn can wait on
the person for a permission), `session/cancel` is handled on the reading thread, and the
client's answers to the agent's own requests are handed to whoever waits for them. A prompt's
cancel token is made on the reading thread when the request is read, so a cancel read right
after it is never lost, however late the turn's thread starts. Standard library only.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Final, TextIO
from urllib.parse import urlsplit

from ahp_host_acp.reference.chat import (
    Cancelled,
    ChatClient,
    Completion,
    EndpointError,
    ToolCallDraft,
    redact,
)
from ahp_host_acp.reference.mcp import McpCancelled, McpClient, McpError, from_acp_entry

#: Seconds a model request may stay silent (a reasoning model can pause long between chunks).
DEFAULT_TIMEOUT: Final = 300.0
#: Model requests per prompt; a bound so a looping model ends the turn.
DEFAULT_MAX_STEPS: Final = 25
SYSTEM_PROMPT: Final = (
    "You are a coding agent. The session folder is {cwd}. Use the tools you are given when "
    "they help; when a StructuredOutput tool is offered, give your final answer by calling it."
)
#: Largest existing file shown as the old side of a diff, in bytes.
MAX_DIFF_OLD: Final = 1048576

AGENT_NAME: Final = "ahp-acp-openai"
LOG_PREFIX: Final = "ahp-acp-openai: "
DECLINED: Final = "The user declined this call."

PERMISSION_OPTIONS: Final[list[dict[str, str]]] = [
    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
]

WRITE_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write a UTF-8 text file, replacing it if it exists. The user is asked first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to write, absolute or relative to the session folder",
                },
                "content": {"type": "string", "description": "The whole new file content"},
            },
            "required": ["path", "content"],
        },
    },
}

_INVALID_PARAMS: Final = -32602
_METHOD_NOT_FOUND: Final = -32601
_INTERNAL_ERROR: Final = -32603
_CLOSED: Final = {"code": _INTERNAL_ERROR, "message": "the client closed the connection"}
_FUNCTION_NAME: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_STOP_REASONS: Final = {"length": "max_tokens", "content_filter": "refusal"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ReferenceConfig:
    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    timeout: float = DEFAULT_TIMEOUT
    max_steps: int = DEFAULT_MAX_STEPS
    system_prompt: str = SYSTEM_PROMPT


def from_env(environ: Mapping[str, str]) -> ReferenceConfig:
    base_url = environ.get("AHP_OPENAI_BASE_URL", "").strip()
    if not base_url:
        raise ConfigError("AHP_OPENAI_BASE_URL is not set")
    try:
        parsed = urlsplit(base_url)
        usable = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        _ = parsed.port
    except ValueError:
        usable = False
    if not usable:
        raise ConfigError("AHP_OPENAI_BASE_URL must be an http(s) URL")
    model = environ.get("AHP_OPENAI_MODEL", "").strip()
    if not model:
        raise ConfigError("AHP_OPENAI_MODEL is not set")
    timeout = DEFAULT_TIMEOUT
    raw_timeout = environ.get("AHP_OPENAI_TIMEOUT")
    if raw_timeout:
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = 0.0
        if not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError("AHP_OPENAI_TIMEOUT must be a number of seconds above 0")
    max_steps = DEFAULT_MAX_STEPS
    raw_steps = environ.get("AHP_OPENAI_MAX_STEPS")
    if raw_steps:
        try:
            max_steps = int(raw_steps)
        except ValueError:
            max_steps = 0
        if max_steps < 1:
            raise ConfigError("AHP_OPENAI_MAX_STEPS must be a whole number, 1 or more")
    return ReferenceConfig(
        base_url=base_url,
        model=model,
        api_key=environ.get("AHP_OPENAI_API_KEY") or None,
        timeout=timeout,
        max_steps=max_steps,
        system_prompt=environ.get("AHP_OPENAI_SYSTEM") or SYSTEM_PROMPT,
    )


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class _Session:
    id: str
    cwd: str
    history: list[dict[str, Any]]
    chat: ChatClient
    mcp: list[McpClient] = field(default_factory=list)
    #: The cancel token of the current (or last) prompt; a new one per prompt request.
    cancel: threading.Event = field(default_factory=threading.Event)
    running: bool = False
    warned: set[str] = field(default_factory=set)


#: A model-visible tool name -> the MCP client that serves it and the tool's own name.
_Routes = dict[str, tuple[McpClient, str]]


def _content_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "content", "content": {"type": "text", "text": text}}]


def _prompt_text(blocks: Any) -> str:
    parts: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind == "resource_link" and isinstance(block.get("uri"), str):
            name = block.get("name")
            parts.append(
                f"[{name if isinstance(name, str) and name else block['uri']}]({block['uri']})"
            )
        elif kind == "resource":
            resource = block.get("resource")
            if isinstance(resource, dict) and isinstance(resource.get("text"), str):
                parts.append(f"Contents of {resource.get('uri')}:\n{resource['text']}")
    return "\n\n".join(parts)


def _diff(target: str, content: str) -> dict[str, Any] | None:
    """The diff to show for writing `content` to `target`; None if the old file cannot be read."""
    item: dict[str, Any] = {"type": "diff", "path": target, "newText": content}
    if not os.path.lexists(target):
        return item
    try:
        with open(target, "rb") as handle:
            old = handle.read(MAX_DIFF_OLD + 1)
        if len(old) > MAX_DIFF_OLD:
            return None
        item["oldText"] = old.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return item


def _mcp_text(result: Mapping[str, Any]) -> str:
    parts = result.get("content")
    texts: list[str] = []
    for part in parts if isinstance(parts, list) else []:
        if (
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ):
            texts.append(part["text"])
        else:
            texts.append(json.dumps(part))
    if texts:
        return "\n".join(texts)
    if result.get("structuredContent") is not None:
        return json.dumps(result["structuredContent"])
    return "Done."


class Agent:
    def __init__(
        self, config: ReferenceConfig, *, stdin: BinaryIO, stdout: BinaryIO, stderr: TextIO
    ) -> None:
        self._config = config
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._write_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._next_session = 0
        self._next_out = 0
        self._waiters: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._closed = False
        self._client_capabilities: dict[str, Any] = {}

    # -- wire ------------------------------------------------------------------

    def _log(self, text: str) -> None:
        line = LOG_PREFIX + redact(text, self._config.api_key) + "\n"
        with self._log_lock:
            try:
                self._stderr.write(line)
                self._stderr.flush()
            except (OSError, ValueError):
                pass

    def _send(self, message: Mapping[str, Any]) -> bool:
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._write_lock:
            try:
                self._stdout.write(data)
                self._stdout.flush()
            except (OSError, ValueError):
                return False
        return True

    def _update(self, session_id: str, update: Mapping[str, Any]) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": session_id, "update": dict(update)},
            }
        )

    def _ask(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Send a request to the client and wait; the answer holds `result` or `error`."""
        event = threading.Event()
        slot: dict[str, Any] = {}
        with self._lock:
            if self._closed:
                return {"error": dict(_CLOSED)}
            self._next_out += 1
            request_id = f"ref-{self._next_out}"
            self._waiters[request_id] = (event, slot)
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        if self._send(message):
            event.wait()
        else:
            slot["error"] = dict(_CLOSED)
        with self._lock:
            self._waiters.pop(request_id, None)
        return slot

    def _deliver(self, message: Mapping[str, Any]) -> None:
        with self._lock:
            waiter = self._waiters.get(message.get("id"))  # type: ignore[arg-type]
        if waiter is None:
            return
        event, slot = waiter
        if "error" in message:
            error = message["error"]
            slot["error"] = error if isinstance(error, dict) else {"message": str(error)}
        else:
            slot["result"] = message.get("result")
        event.set()

    def serve(self) -> int:
        parsed = urlsplit(self._config.base_url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        self._log(f"model {self._config.model} at {parsed.scheme}://{host}{port}")
        try:
            for line in iter(self._stdin.readline, b""):
                try:
                    message = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                method = message.get("method")
                if isinstance(method, str):
                    if "id" in message:
                        armed = self._arm(message) if method == "session/prompt" else None
                        threading.Thread(
                            target=self._handle, args=(message, armed), daemon=True
                        ).start()
                    elif method == "session/cancel":
                        params = message.get("params")
                        self._cancel(params if isinstance(params, dict) else {})
                elif "id" in message:
                    self._deliver(message)
        except OSError:
            pass
        self._shutdown()
        return 0

    def _shutdown(self) -> None:
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            waiters = list(self._waiters.values())
        for session in sessions:
            session.cancel.set()
            session.chat.abort()
        for event, slot in waiters:
            slot["error"] = dict(_CLOSED)
            event.set()
        for session in sessions:
            for client in session.mcp:
                client.close()

    # -- requests --------------------------------------------------------------

    def _arm(self, message: Mapping[str, Any]) -> threading.Event | None:
        """On the reading thread: claim the session for this prompt and give it a fresh cancel
        token, before any later `session/cancel` is read. None when the prompt is refused."""
        params = message.get("params")
        session_id = params.get("sessionId") if isinstance(params, dict) else None
        with self._lock:
            session = self._sessions.get(session_id) if isinstance(session_id, str) else None
            if session is None or session.running:
                return None
            session.running = True
            session.cancel = threading.Event()
            return session.cancel

    def _handle(self, message: dict[str, Any], armed: threading.Event | None = None) -> None:
        params = message.get("params")
        method = str(message["method"])
        try:
            result = self._dispatch(method, params if isinstance(params, dict) else {}, armed)
        except _RpcError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "error": error}
        except Exception as exc:
            self._log(f"{method} failed: {exc}")
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": _INTERNAL_ERROR, "message": str(exc)},
            }
        else:
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        self._send(reply)

    def _dispatch(
        self, method: str, params: dict[str, Any], armed: threading.Event | None = None
    ) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "session/new":
            return self._new_session(params)
        if method == "session/prompt":
            return self._prompt(params, armed)
        if method == "authenticate":
            return {}
        raise _RpcError(_METHOD_NOT_FOUND, f"Method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        capabilities = params.get("clientCapabilities")
        self._client_capabilities = capabilities if isinstance(capabilities, dict) else {}
        try:
            version = importlib.metadata.version("ahp-host-acp")
        except importlib.metadata.PackageNotFoundError:
            version = "0"
        return {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
                "mcpCapabilities": {"http": False, "sse": False},
            },
            "authMethods": [],
            "agentInfo": {
                "name": AGENT_NAME,
                "title": "OpenAI-compatible reference agent",
                "version": version,
            },
        }

    def _new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            raise _RpcError(_INVALID_PARAMS, "cwd must be an absolute path")
        cwd = os.path.normpath(cwd)
        with self._lock:
            self._next_session += 1
            session_id = f"ref-{self._next_session}"
        config = self._config
        session = _Session(
            id=session_id,
            cwd=cwd,
            history=[{"role": "system", "content": config.system_prompt.replace("{cwd}", cwd)}],
            chat=ChatClient(config.base_url, config.model, config.api_key, config.timeout),
        )
        entries = params.get("mcpServers")
        for entry in entries if isinstance(entries, list) else []:
            name = entry.get("name") if isinstance(entry, dict) else None
            label = name if isinstance(name, str) else "?"
            client = from_acp_entry(entry) if isinstance(entry, dict) else None
            if client is None:
                self._log(f"MCP server {label} left out: only stdio is supported")
                continue
            try:
                client.start()
            except (McpError, OSError) as exc:
                self._log(f"MCP server {label} did not start: {exc}")
                client.close()
                continue
            session.mcp.append(client)
        with self._lock:
            self._sessions[session_id] = session
        return {"sessionId": session_id}

    def _cancel(self, params: dict[str, Any]) -> None:
        with self._lock:
            session = self._sessions.get(str(params.get("sessionId")))
        if session is not None:
            session.cancel.set()
            session.chat.abort()

    def _prompt(self, params: dict[str, Any], armed: threading.Event | None) -> dict[str, Any]:
        session_id = params.get("sessionId")
        with self._lock:
            session = self._sessions.get(session_id) if isinstance(session_id, str) else None
            if session is None:
                raise _RpcError(_INVALID_PARAMS, f"unknown session {session_id}")
            if armed is None:
                raise _RpcError(_INTERNAL_ERROR, "a prompt is already running in this session")
        try:
            return self._turn(session, _prompt_text(params.get("prompt")), armed)
        finally:
            with self._lock:
                session.running = False

    # -- a turn ----------------------------------------------------------------

    def _warn_once(self, session: _Session, text: str) -> None:
        if text not in session.warned:
            session.warned.add(text)
            self._log(text)

    def _tools(self, session: _Session) -> tuple[list[dict[str, Any]], _Routes]:
        tools: list[dict[str, Any]] = [WRITE_TOOL]
        routes: _Routes = {}
        taken = {"write_file"}
        for client in session.mcp:
            try:
                listed = client.list_tools()
            except McpError as exc:
                self._warn_once(session, f"MCP server {client.name} could not list tools: {exc}")
                continue
            prefix = "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", client.name) + "__"
            for tool in listed:
                name = prefix + tool["name"]
                if not _FUNCTION_NAME.fullmatch(name):
                    self._warn_once(session, f"MCP tool {name} left out: not a valid tool name")
                    continue
                if name in taken:
                    self._warn_once(session, f"MCP tool {name} left out: the name is taken")
                    continue
                taken.add(name)
                schema = tool.get("inputSchema")
                described = tool.get("description") or tool.get("title") or tool["name"]
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": str(described),
                            "parameters": (
                                schema
                                if isinstance(schema, dict)
                                else {"type": "object", "properties": {}}
                            ),
                        },
                    }
                )
                routes[name] = (client, tool["name"])
        return tools, routes

    def _stream(
        self, session: _Session, tools: list[dict[str, Any]], cancel: threading.Event
    ) -> tuple[Completion | None, str]:
        """One model request; (None, the partial text) when the turn was cancelled."""
        chunks: list[str] = []

        def on_text(text: str) -> None:
            chunks.append(text)
            self._update(
                session.id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            )

        def on_reasoning(text: str) -> None:
            self._update(
                session.id,
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": text},
                },
            )

        try:
            done = session.chat.stream(
                session.history,
                tools,
                on_text=on_text,
                on_reasoning=on_reasoning,
                cancelled=cancel,
            )
        except Cancelled:
            return None, "".join(chunks)
        return done, ""

    def _turn(self, session: _Session, text: str, cancel: threading.Event) -> dict[str, Any]:
        history = session.history
        start = len(history)
        history.append({"role": "user", "content": text})
        totals = {"prompt": 0, "completion": 0, "cached": 0, "total": 0}
        counted = False

        def finish(reason: str) -> dict[str, Any]:
            result: dict[str, Any] = {"stopReason": reason}
            if counted:
                result["usage"] = {
                    "inputTokens": max(0, totals["prompt"] - totals["cached"]),
                    "outputTokens": totals["completion"],
                    "cachedReadTokens": totals["cached"],
                    "totalTokens": totals["total"],
                }
            return result

        for _ in range(self._config.max_steps):
            if cancel.is_set():
                return finish("cancelled")
            tools, routes = self._tools(session)
            try:
                done, partial = self._stream(session, tools, cancel)
            except EndpointError as exc:
                del history[start:]
                data = {"httpStatus": exc.status} if exc.status is not None else None
                raise _RpcError(_INTERNAL_ERROR, exc.message, data) from exc
            if done is None:
                if partial:
                    history.append({"role": "assistant", "content": partial})
                return finish("cancelled")
            if done.usage is not None:
                counted = True
                for key in totals:
                    totals[key] += done.usage[key]
            reply: dict[str, Any] = {"role": "assistant", "content": done.text or None}
            if done.tool_calls:
                reply["tool_calls"] = [
                    {
                        "id": d.id,
                        "type": "function",
                        "function": {"name": d.name, "arguments": d.arguments},
                    }
                    for d in done.tool_calls
                ]
            history.append(reply)
            if not done.tool_calls:
                return finish(_STOP_REASONS.get(done.finish_reason or "", "end_turn"))
            stopped = False
            for draft in done.tool_calls:
                if stopped or cancel.is_set():
                    stopped = True
                    result_text = "Cancelled."
                else:
                    result_text, stopped = self._run_call(session, routes, draft, cancel)
                history.append({"role": "tool", "tool_call_id": draft.id, "content": result_text})
            if stopped or cancel.is_set():
                return finish("cancelled")
        return finish("max_turn_requests")

    # -- tools -----------------------------------------------------------------

    def _failed(
        self,
        session: _Session,
        call_id: str,
        title: str,
        kind: str,
        text: str,
        raw_input: Any = None,
    ) -> str:
        """Report a call that never reached the person; the text is also the tool message."""
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call",
            "toolCallId": call_id,
            "title": title,
            "kind": kind,
            "status": "failed",
            "content": _content_text(text),
        }
        if raw_input is not None:
            update["rawInput"] = raw_input
        self._update(session.id, update)
        return text

    def _end(
        self, session: _Session, call_id: str, status: str, content: list[dict[str, Any]]
    ) -> None:
        self._update(
            session.id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": status,
                "content": content,
            },
        )

    def _run_call(
        self,
        session: _Session,
        routes: _Routes,
        draft: ToolCallDraft,
        cancel: threading.Event | None = None,
    ) -> tuple[str, bool]:
        """Run one tool call; the text for the tool message and whether the turn must stop."""
        name = draft.name
        try:
            arguments = json.loads(draft.arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("the arguments must be a JSON object")
        except ValueError as exc:
            return self._failed(
                session, draft.id, name, "other", f"Invalid arguments: {exc}"
            ), False
        if name == "write_file":
            return self._write(session, draft.id, arguments)
        route = routes.get(name)
        if route is None:
            return self._failed(
                session, draft.id, name, "other", f"Unknown tool: {name}", arguments
            ), False
        call: dict[str, Any] = {
            "toolCallId": draft.id,
            "title": name,
            "kind": "other",
            "status": "pending",
            "rawInput": arguments,
        }
        outcome = self._permission(session, call)
        if outcome != "allow":
            return self._refused(session, draft.id, outcome), outcome == "cancelled"
        self._end(session, draft.id, "in_progress", [])
        client, tool = route
        try:
            result = client.call_tool(tool, arguments, cancel=cancel)
        except McpCancelled:
            self._end(session, draft.id, "failed", _content_text("Cancelled."))
            return "Cancelled.", True
        except McpError as exc:
            text = f"The MCP server {client.name} failed: {exc}"
            self._end(session, draft.id, "failed", _content_text(text))
            return text, False
        text = _mcp_text(result)
        status = "failed" if result.get("isError") is True else "completed"
        self._end(session, draft.id, status, _content_text(text))
        return text, False

    def _permission(self, session: _Session, call: dict[str, Any]) -> str:
        """Ask the client; 'allow', 'cancelled' or 'reject' (a refusal or an error answer)."""
        answer = self._ask(
            "session/request_permission",
            {"sessionId": session.id, "toolCall": call, "options": PERMISSION_OPTIONS},
        )
        result = answer.get("result")
        outcome = result.get("outcome") if isinstance(result, dict) else None
        if isinstance(outcome, dict):
            if outcome.get("outcome") == "cancelled":
                return "cancelled"
            if outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow_once":
                return "allow"
        return "reject"

    def _refused(self, session: _Session, call_id: str, outcome: str) -> str:
        text = "Cancelled." if outcome == "cancelled" else DECLINED
        self._end(session, call_id, "failed", _content_text(text))
        return text

    def _write(
        self, session: _Session, call_id: str, arguments: dict[str, Any]
    ) -> tuple[str, bool]:
        path, content = arguments.get("path"), arguments.get("content")
        if not isinstance(path, str) or not path or not isinstance(content, str):
            text = "Invalid arguments: path and content must be strings, path not empty"
            return self._failed(session, call_id, "write_file", "edit", text, arguments), False
        title = f"Write {path}"
        # Confinement is checked on real paths: realpath follows every symlink in the target (or,
        # for a new file, in its nearest existing parent), so a link inside the folder that points
        # outside it is refused. What is shown and written is the resolved path.
        cwd = os.path.realpath(session.cwd)
        lexical = os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))
        target = os.path.realpath(lexical)
        try:
            inside = os.path.normcase(os.path.commonpath([cwd, target])) == os.path.normcase(cwd)
        except ValueError:
            inside = False
        if not inside:
            text = f"Refused: {target} is outside the session folder {cwd}"
            return self._failed(session, call_id, title, "edit", text, arguments), False
        diff = _diff(target, content)
        call: dict[str, Any] = {
            "toolCallId": call_id,
            "title": title,
            "kind": "edit",
            "status": "pending",
            "rawInput": arguments,
            "locations": [{"path": target}],
        }
        if diff is not None:
            call["content"] = [diff]
        self._update(session.id, {"sessionUpdate": "tool_call", **call})
        outcome = self._permission(session, call)
        if outcome != "allow":
            return self._refused(session, call_id, outcome), outcome == "cancelled"
        self._end(session, call_id, "in_progress", [])
        error: str | None = None
        if self._client_capabilities.get("fs", {}).get("writeTextFile") is True:
            answer = self._ask(
                "fs/write_text_file",
                {"sessionId": session.id, "path": target, "content": content},
            )
            failure = answer.get("error")
            if failure is not None:
                message = failure.get("message") if isinstance(failure, dict) else failure
                error = f"The client could not write {target}: {message}"
        else:
            try:
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).write_text(content, encoding="utf-8", newline="")
            except OSError as exc:
                error = f"Could not write {target}: {exc}"
        if error is not None:
            self._end(session, call_id, "failed", _content_text(error))
            return error, False
        text = f"Wrote {len(content)} characters to {target}"
        shown = ([diff] if diff is not None else []) + _content_text(text)
        self._end(session, call_id, "completed", shown)
        return text, False
