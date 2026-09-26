"""The account's other Claude Code sessions, driven through claude.ai.

Every Claude Code session with Remote Control on - in a terminal, the desktop
app, an IDE, on any machine - is also a session on claude.ai, and claude.ai's
own apps read and drive it through a handful of endpoints. This module uses
the same ones, so this host can list those sessions and take part in them
like the phone does: send messages, stop a turn, answer approvals.

None of it is documented or promised to stay put. What each call is, as
Claude Code 2.1.281 and the claude.ai apps use it:

- ``GET  /v1/code/sessions?limit=&cursor=`` -- the account's sessions. A
  Remote Control one has ``environment_kind == "bridge"``.
- ``GET  /v1/code/sessions/{id}/events?limit=&sort_order=desc`` -- its
  transcript, newest first; ``limit=1`` finds the current sequence number.
- ``GET  /v1/code/sessions/{id}/events/stream?from_sequence_num=`` -- SSE:
  ``client_event`` frames whose data is ``{sequence_num, event_type, source,
  payload}``, the payload a Claude Code stream-json message. With
  ``control_only=1`` it carries the control traffic instead - approvals are
  only there, so both streams are read.
- ``POST /v1/code/sessions/{id}/events`` with ``{"events": [{"payload": ...}]}``
  -- input: a user message, an interrupt, an answer to an approval.

`RemoteClient` puts that behind the same interface as the Agent SDK's client,
so a mirrored session is an ordinary `ClaudeSession`: turns typed elsewhere,
approvals answered on either side, the mode switched on the phone - all of it
already handled there.

The login is Claude Code's own, read (never written) from where Claude Code
keeps it. Its refresh tokens rotate, so this never refreshes: a refresh here
would log Claude Code out. Claude Code keeps it fresh as it runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
from claude_agent_sdk import (
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)
from claude_agent_sdk._internal.message_parser import parse_message

log = logging.getLogger(__name__)

BASE_URL: Final = "https://api.anthropic.com"
_HEADERS: Final = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "environments-2025-11-01",
    "x-environment-runner-version": "cli",
}
#: Where Claude Code keeps its login: the macOS Keychain, else this file.
KEYCHAIN_SERVICE: Final = "Claude Code-credentials"
CREDENTIALS_FILE: Final = Path.home() / ".claude" / ".credentials.json"
#: Re-read the login this often at most; it changes only when Claude Code
#: refreshes it.
_REREAD_S: Final = 60.0


class RemoteError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LoginError(RemoteError):
    """No usable claude.ai login: sign in to Claude Code on this machine."""


# -- the login ---------------------------------------------------------------


def _read_keychain() -> str | None:
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _read_file() -> str | None:
    try:
        return CREDENTIALS_FILE.read_text()
    except OSError:
        return None


def read_login() -> tuple[str, float] | None:
    """Claude Code's access token and when it expires (epoch seconds)."""
    raw = _read_keychain() if sys.platform == "darwin" else None
    raw = raw or _read_file()
    if not raw:
        return None
    try:
        oauth = json.loads(raw).get("claudeAiOauth") or {}
    except ValueError:
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    expires = oauth.get("expiresAt")
    return token, float(expires) / 1000 if isinstance(expires, int | float) else 0.0


class Login:
    """Claude Code's login, re-read as Claude Code refreshes it."""

    def __init__(self, reader: Callable[[], tuple[str, float] | None] = read_login) -> None:
        self._reader = reader
        self._token: str | None = None
        self._expires = 0.0
        self._read_at = 0.0

    async def token(self, *, reread: bool = False) -> str:
        now = time.time()
        stale = now - self._read_at > _REREAD_S or now >= self._expires - 60
        if reread or self._token is None or stale:
            found = await asyncio.to_thread(self._reader)
            self._read_at = now
            if found is not None:
                self._token, self._expires = found
        if self._token is None:
            raise LoginError("no claude.ai login: sign in to Claude Code on this machine")
        return self._token


# -- the API -----------------------------------------------------------------


@dataclass(frozen=True)
class RemoteSession:
    """One row of the session list, as much of it as this host uses."""

    id: str
    title: str
    #: ``bridge`` for Remote Control; ``anthropic_cloud`` for cloud sessions.
    environment_kind: str
    #: ``connected`` / ``disconnected``: whether its machine is attached.
    connection_status: str
    #: ``active`` / ``archived``.
    status: str
    #: ``running`` / ``idle`` / ``requires_action``.
    worker_status: str

    @property
    def live(self) -> bool:
        """A Remote Control session whose machine is there to drive it."""
        return (
            self.environment_kind == "bridge"
            and self.status == "active"
            and self.connection_status == "connected"
        )

    @classmethod
    def from_wire(cls, row: Mapping[str, Any]) -> RemoteSession:
        return cls(
            id=str(row["id"]),
            title=str(row.get("title") or "Untitled"),
            environment_kind=str(row.get("environment_kind") or ""),
            connection_status=str(row.get("connection_status") or ""),
            status=str(row.get("status") or ""),
            worker_status=str(row.get("worker_status") or ""),
        )


@dataclass(frozen=True)
class Event:
    sequence_num: int | None
    #: ``client`` (a claude.ai app, or this host) or ``worker`` (Claude Code).
    source: str | None
    payload: Mapping[str, Any]

    @classmethod
    def from_wire(cls, row: Mapping[str, Any], event_id: str | None = None) -> Event | None:
        payload = row.get("payload")
        if not isinstance(payload, Mapping) or not isinstance(payload.get("type"), str):
            return None
        seq = row.get("sequence_num")
        if seq is None and event_id is not None and event_id.isdigit():
            seq = event_id
        try:
            sequence_num = int(seq) if seq is not None else None
        except (TypeError, ValueError):
            sequence_num = None
        source = row.get("source")
        return cls(sequence_num, source if isinstance(source, str) else None, payload)


class Api:
    """Authenticated calls to claude.ai's Remote Control endpoints."""

    def __init__(
        self, login: Login | None = None, *, http: httpx.AsyncClient | None = None
    ) -> None:
        self._login = login or Login()
        self._http = http or httpx.AsyncClient(
            base_url=BASE_URL, timeout=httpx.Timeout(30, read=60)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _headers(self, *, reread: bool = False) -> dict[str, str]:
        token = await self._login.token(reread=reread)
        return {"Authorization": f"Bearer {token}", **_HEADERS}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._http.request(method, path, headers=await self._headers(), **kwargs)
        if response.status_code == 401:
            # Claude Code may have refreshed the login since it was read.
            response = await self._http.request(
                method, path, headers=await self._headers(reread=True), **kwargs
            )
            if response.status_code == 401:
                raise LoginError("claude.ai refused the login; run Claude Code to refresh it", 401)
        if response.status_code >= 400:
            raise RemoteError(
                f"{method} {path}: HTTP {response.status_code} {response.text[:300]}",
                response.status_code,
            )
        return response.json() if response.content else None

    async def sessions(self, *, max_pages: int = 5) -> list[RemoteSession]:
        rows: list[RemoteSession] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params = {"limit": "100", **({"cursor": cursor} if cursor else {})}
            page = await self._request("GET", "/v1/code/sessions", params=params)
            rows.extend(RemoteSession.from_wire(row) for row in page.get("data") or ())
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return rows

    async def recent(self, session_id: str, limit: int) -> list[Event]:
        """The newest *limit* events, newest first."""
        page = await self._request(
            "GET",
            f"/v1/code/sessions/{session_id}/events",
            params={"limit": str(limit), "sort_order": "desc"},
        )
        events = (Event.from_wire(row) for row in page.get("data") or ())
        return [event for event in events if event is not None]

    async def stream(
        self, session_id: str, from_sequence_num: int, *, control: bool = False
    ) -> AsyncIterator[Event]:
        """Events after *from_sequence_num*, until the server closes the stream."""
        params = {"from_sequence_num": str(from_sequence_num)}
        if control:
            params["control_only"] = "1"
        headers = {**await self._headers(), "Accept": "text/event-stream"}
        async with self._http.stream(
            "GET",
            f"/v1/code/sessions/{session_id}/events/stream",
            params=params,
            headers=headers,
            timeout=httpx.Timeout(30, read=90),
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode(errors="replace")
                raise RemoteError(
                    f"stream {session_id}: HTTP {response.status_code} {body[:300]}",
                    response.status_code,
                )
            async for name, event_id, data in _sse(response.aiter_lines()):
                if name != "client_event":
                    continue
                try:
                    row = json.loads(data)
                except ValueError:
                    continue
                event = Event.from_wire(row, event_id)
                if event is not None:
                    yield event

    async def post(self, session_id: str, payload: Mapping[str, Any]) -> None:
        await self._request(
            "POST",
            f"/v1/code/sessions/{session_id}/events",
            json={"events": [{"payload": dict(payload)}]},
        )


async def _sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str | None, str]]:
    """(event, id, data) for each server-sent event."""
    name, event_id, data = "message", None, list[str]()
    async for line in lines:
        if line == "":
            if data:
                yield name, event_id, "\n".join(data)
            name, event_id, data = "message", None, []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)
            elif field == "id":
                event_id = value


# -- a session, behind the Agent SDK's client interface ---------------------------

#: How many recent exchanges a newly listed session shows.
BACKFILL_EXCHANGES: Final = 2
#: Control requests that wait on a person. The rest is plumbing between
#: Claude Code and claude.ai.
_ASKS: Final = frozenset({"can_use_tool"})


def _user_text(payload: Mapping[str, Any]) -> bool:
    """A message someone typed, as opposed to a tool result."""
    if payload.get("type") != "user" or payload.get("parent_tool_use_id"):
        return False
    content = (payload.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(block, Mapping) and block.get("type") == "text" for block in content
    )


class RemoteClient:
    """One claude.ai session, looking like `ClaudeSDKClient` to `ClaudeSession`.

    Output is both streams, parsed into the SDK's message types. A message
    typed elsewhere carries an ``origin`` (``human``) so `ClaudeSession`
    opens a turn for it; one sent from here comes back with the uuid it was
    sent with, and is recognised by that. An approval on the control stream
    goes to ``options.can_use_tool``; when another client answers first, the
    pending call is cancelled - which is how the SDK reports the same thing.
    """

    def __init__(
        self,
        api: Api,
        session_id: str,
        options: ClaudeAgentOptions,
        *,
        backfill: int = 0,
        reconnect_s: float = 2.0,
    ) -> None:
        self._api = api
        self.session_id = session_id
        self._options = options
        self._backfill = backfill
        self._reconnect_s = reconnect_s
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._asks: dict[str, asyncio.Task[None]] = {}
        self._seen_asks: set[str] = set()
        self._seen_messages: set[str] = set()

    async def connect(self) -> None:
        latest = await self._api.recent(self.session_id, 1)
        current = (latest[0].sequence_num or 0) if latest else 0
        start = await self._backfill_from(current) if self._backfill else current
        self._tasks = [
            asyncio.create_task(self._follow(start, control=False)),
            # From now, never earlier: an old approval replayed would be raised
            # again as if it were new.
            asyncio.create_task(self._follow(current, control=True)),
        ]

    async def _backfill_from(self, current: int) -> int:
        """Where to start for the last few exchanges to be shown."""
        events = await self._api.recent(self.session_id, 100)
        typed = [e for e in events if _user_text(e.payload) and e.sequence_num is not None]
        if not typed:
            return current
        oldest = typed[: self._backfill][-1]
        assert oldest.sequence_num is not None
        return oldest.sequence_num - 1

    async def _follow(self, start: int, *, control: bool) -> None:
        position = start
        while True:
            try:
                async for event in self._api.stream(self.session_id, position, control=control):
                    # `task_summary` and friends carry no sequence number: the
                    # resume point moves only on those that do.
                    if event.sequence_num is not None:
                        position = max(position, event.sequence_num)
                    if control:
                        await self._on_control(event)
                    else:
                        self._on_event(event)
            except asyncio.CancelledError:
                raise
            except LoginError:
                log.warning("claude.ai session %s: no usable login", self.session_id)
            except Exception:
                log.debug("claude.ai stream for %s dropped", self.session_id, exc_info=True)
            await asyncio.sleep(self._reconnect_s)

    def _on_event(self, event: Event) -> None:
        payload = dict(event.payload)
        if payload.get("type") in ("control_request", "control_response"):
            return
        if _user_text(payload):
            message_id = payload.get("uuid")
            if isinstance(message_id, str):
                if message_id in self._seen_messages:
                    return
                self._seen_messages.add(message_id)
            if payload.get("origin") is None and event.source == "client":
                # Typed in a claude.ai app. Our own posts come back this way
                # too; `ClaudeSession` tells them apart by uuid.
                payload["origin"] = {"kind": "human"}
        try:
            message = parse_message(payload)
        except Exception:
            log.debug("unparsed claude.ai event %s", payload.get("type"), exc_info=True)
            return
        if message is not None:
            self._queue.put_nowait(message)

    async def _on_control(self, event: Event) -> None:
        payload = event.payload
        if payload.get("type") == "control_request":
            request = payload.get("request") or {}
            request_id = payload.get("request_id")
            if request.get("subtype") not in _ASKS or not isinstance(request_id, str):
                return
            if request_id in self._seen_asks:
                return
            self._seen_asks.add(request_id)
            self._asks[request_id] = asyncio.create_task(self._ask(request_id, request))
        elif payload.get("type") == "control_response":
            response = payload.get("response") or {}
            request_id = response.get("request_id")
            ask = self._asks.pop(request_id, None) if isinstance(request_id, str) else None
            if ask is not None and not ask.done():
                ask.cancel()  # answered somewhere else

    async def _ask(self, request_id: str, request: Mapping[str, Any]) -> None:
        can_use_tool = self._options.can_use_tool
        if can_use_tool is None:
            return
        tool_input = request.get("input") or {}
        context = ToolPermissionContext(
            tool_use_id=request.get("tool_use_id"),
            title=request.get("title"),
            display_name=request.get("display_name"),
            description=request.get("description"),
        )
        try:
            result = await can_use_tool(str(request.get("tool_name")), dict(tool_input), context)
        except asyncio.CancelledError:
            return
        finally:
            self._asks.pop(request_id, None)
        if isinstance(result, PermissionResultAllow):
            decision: dict[str, Any] = {
                "behavior": "allow",
                "updatedInput": result.updated_input
                if result.updated_input is not None
                else tool_input,
            }
        elif isinstance(result, PermissionResultDeny):
            decision = {"behavior": "deny", "message": result.message}
        else:
            return
        await self._api.post(
            self.session_id,
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": decision,
                },
            },
        )

    # -- the SdkClient interface -------------------------------------------

    async def query(self, prompt: str | AsyncIterable[dict[str, Any]]) -> None:
        if isinstance(prompt, str):
            messages: list[dict[str, Any]] = [
                {
                    "type": "user",
                    "uuid": str(uuid.uuid4()),
                    "message": {"role": "user", "content": prompt},
                    "parent_tool_use_id": None,
                }
            ]
        else:
            messages = [message async for message in prompt]
        for message in messages:
            payload = {**message, "session_id": self.session_id}
            # A priority is the local CLI's queue slot; claude.ai queues on its own.
            payload.pop("priority", None)
            await self._api.post(self.session_id, payload)

    def receive_messages(self) -> AsyncIterator[Any]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[Any]:
        while True:
            yield await self._queue.get()

    async def _control(self, request: Mapping[str, Any]) -> None:
        await self._api.post(
            self.session_id,
            {
                "type": "control_request",
                "request_id": str(uuid.uuid4()),
                "request": dict(request),
            },
        )

    async def interrupt(self) -> None:
        await self._control({"subtype": "interrupt"})

    async def set_model(self, model: str | None = None) -> None:
        await self._control({"subtype": "set_model", "model": model})

    async def set_permission_mode(self, mode: Any) -> None:
        await self._control({"subtype": "set_permission_mode", "mode": mode})

    async def get_server_info(self) -> dict[str, Any] | None:
        return None

    async def remote_control(
        self, enabled: bool, *, reattach: str | None = None, keep: bool = True
    ) -> Mapping[str, Any]:
        return {}  # it already is a Remote Control session; that is how it is here

    async def disconnect(self) -> None:
        tasks = [*self._tasks, *self._asks.values()]
        self._tasks, self._asks = [], {}
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def uri_of(session_id: str) -> str:
    """The host's URI for a mirrored claude.ai session."""
    return f"ahp-session:/claude-ai-{session_id}"
