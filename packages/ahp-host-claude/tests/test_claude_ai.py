"""The account's other sessions, through claude.ai: a fake API, no network."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_host_server.provider.base import AgentSessionContext, UserMessage
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    ToolPermissionContext,
)
from claude_agent_sdk import UserMessage as SdkUserMessage

from agent_host_server_claude.claude_ai import (
    Api,
    Event,
    Login,
    LoginError,
    RemoteClient,
    RemoteSession,
    _user_text,
    uri_of,
)
from agent_host_server_claude.provider import ClaudeProvider, ClaudeSession
from tests.fakes import FakePublisher, RecordingSink, eventually


def _event(seq: int | None, payload: dict[str, Any], source: str = "worker") -> Event:
    return Event(seq, source, payload)


def _typed(seq: int, text: str, uuid: str, source: str = "client") -> Event:
    return _event(
        seq,
        {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}},
        source,
    )


def _assistant(seq: int, text: str) -> Event:
    return _event(
        seq,
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "m",
                "content": [{"type": "text", "text": text}],
            },
            "parent_tool_use_id": None,
        },
    )


def _result(seq: int) -> Event:
    return _event(
        seq,
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "is_error": False,
            "num_turns": 1,
            "session_id": "local",
        },
    )


class FakeApi:
    """claude.ai: a history, two live streams to push into, and what was posted."""

    def __init__(self, history: list[Event] | None = None) -> None:
        self.history = list(history or [])  # oldest first
        self.rows: list[RemoteSession] = []
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.started_at: dict[bool, int] = {}
        self._live: dict[bool, asyncio.Queue[Event]] = {
            False: asyncio.Queue(),
            True: asyncio.Queue(),
        }
        self.closed = False

    def push(self, event: Event, *, control: bool = False) -> None:
        self._live[control].put_nowait(event)

    async def sessions(self) -> list[RemoteSession]:
        return list(self.rows)

    async def recent(self, session_id: str, limit: int) -> list[Event]:
        return list(reversed(self.history))[:limit]

    async def typed(self, session_id: str, want: int, *, max_events: int = 3000) -> list[int]:
        newest_first = list(reversed(self.history))[:max_events]
        found = [
            e.sequence_num
            for e in newest_first
            if e.sequence_num is not None and _user_text(e.payload)
        ]
        return found[:want]

    async def stream(
        self, session_id: str, from_sequence_num: int, *, control: bool = False
    ) -> AsyncIterator[Event]:
        self.started_at[control] = from_sequence_num
        if not control:
            for event in self.history:
                if event.sequence_num is not None and event.sequence_num > from_sequence_num:
                    yield event
        while True:
            yield await self._live[control].get()

    async def post(self, session_id: str, payload: Mapping[str, Any]) -> None:
        self.posted.append((session_id, dict(payload)))

    async def aclose(self) -> None:
        self.closed = True


def _client(api: FakeApi, **kwargs: Any) -> tuple[RemoteClient, ClaudeAgentOptions]:
    options = ClaudeAgentOptions()
    return RemoteClient(api, "cse_1", options, reconnect_s=0.01, **kwargs), options  # type: ignore[arg-type]


async def _next(client: RemoteClient) -> Any:
    return await asyncio.wait_for(anext(client.receive_messages()), 1)


# -- the login -----------------------------------------------------------------


async def test_the_login_is_claude_codes_own_and_is_reread_when_it_may_have_changed() -> None:
    reads: list[int] = []
    tokens = iter(["old", "new"])

    def reader() -> tuple[str, float]:
        reads.append(1)
        return next(tokens), 9e12

    login = Login(reader)
    assert await login.token() == "old"
    assert await login.token() == "old", "re-read on every call"
    assert await login.token(reread=True) == "new"
    assert len(reads) == 2


async def test_no_login_says_how_to_get_one() -> None:
    with pytest.raises(LoginError, match="sign in to Claude Code"):
        await Login(lambda: None).token()


async def test_a_refused_token_is_reread_once_before_giving_up() -> None:
    """Claude Code may have refreshed the login since this host read it."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    tokens = iter(["old", "new"])
    api = Api(
        Login(lambda: (next(tokens), 9e12)),
        http=httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)),
    )
    assert await api.sessions() == []
    assert seen == ["Bearer old", "Bearer new"]
    await api.aclose()


async def test_requests_carry_what_claude_ai_expects() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    api = Api(
        Login(lambda: ("t", 9e12)),
        http=httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)),
    )
    await api.post("cse_1", {"type": "user"})
    [request] = captured
    assert request.url.path == "/v1/code/sessions/cse_1/events"
    assert json.loads(request.content) == {"events": [{"payload": {"type": "user"}}]}
    assert request.headers["anthropic-beta"] == "environments-2025-11-01"
    await api.aclose()


# -- the session list ----------------------------------------------------------


def test_only_a_connected_remote_control_session_is_live() -> None:
    def row(**over: str) -> RemoteSession:
        base = {
            "id": "cse_1",
            "environment_kind": "bridge",
            "status": "active",
            "connection_status": "connected",
        }
        return RemoteSession.from_wire({**base, **over})

    assert row().live
    assert not row(environment_kind="anthropic_cloud").live
    assert not row(status="archived").live
    assert not row(connection_status="disconnected").live


# -- one session -----------------------------------------------------------------


async def test_both_streams_start_now_so_old_approvals_are_not_raised_again() -> None:
    api = FakeApi([_typed(5, "hi", "u1"), _assistant(6, "hello")])
    client, _ = _client(api)
    await client.connect()
    await eventually(lambda: len(api.started_at) == 2)
    assert api.started_at == {False: 6, True: 6}
    await client.disconnect()


async def test_a_new_listing_shows_the_last_exchanges() -> None:
    api = FakeApi(
        [
            _typed(1, "first", "u1"),
            _assistant(2, "one"),
            _typed(3, "second", "u2"),
            _assistant(4, "two"),
            _typed(5, "third", "u3"),
            _assistant(6, "three"),
        ]
    )
    client, _ = _client(api, backfill=2)
    await client.connect()
    await eventually(lambda: False in api.started_at)
    assert api.started_at[False] == 2, "should start just before the second-last message"
    assert api.started_at[True] == 6
    await client.disconnect()


async def test_history_is_found_behind_an_idle_sessions_housekeeping() -> None:
    """Left idle, a session's recent events are all control and system traffic."""
    control = [
        _event(n, {"type": "control_request", "request": {"subtype": "noop"}})
        for n in range(3, 500)
    ]
    api = FakeApi([_typed(1, "the question", "u1"), _assistant(2, "the answer"), *control])
    client, _ = _client(api, backfill=5)
    await client.connect()
    await eventually(lambda: False in api.started_at)
    assert api.started_at[False] == 0
    assert isinstance(await _next(client), SdkUserMessage)
    await client.disconnect()


async def test_real_paging_walks_back_through_the_log() -> None:
    pages = {
        None: {
            "data": [{"sequence_num": n, "payload": {"type": "system"}} for n in (9, 8, 7)],
            "next_cursor": "p2",
        },
        "p2": {
            "data": [
                {
                    "sequence_num": 6,
                    "payload": {
                        "type": "user",
                        "uuid": "u",
                        "message": {"role": "user", "content": "hi"},
                    },
                }
            ],
            "next_cursor": None,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    api = Api(
        Login(lambda: ("t", 9e12)),
        http=httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)),
    )
    assert await api.typed("cse_1", 5) == [6]
    await api.aclose()


async def test_a_message_typed_in_an_app_is_marked_as_from_elsewhere() -> None:
    api = FakeApi()
    client, _ = _client(api)
    await client.connect()
    api.push(_typed(1, "from my phone", "phone-1"))
    api.push(_typed(1, "from my phone", "phone-1"))  # a reconnect replays it
    api.push(_assistant(2, "ok"))
    message = await _next(client)
    assert isinstance(message, SdkUserMessage)
    assert message.origin == {"kind": "human"}
    assert isinstance(await _next(client), AssistantMessage), "the replay was not dropped"
    await client.disconnect()


async def test_sending_posts_a_user_message_under_its_own_uuid() -> None:
    api = FakeApi()
    client, _ = _client(api)

    async def one() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "uuid": "mine",
            "priority": "next",
            "message": {"role": "user", "content": "hi"},
            "parent_tool_use_id": None,
        }

    await client.query(one())
    [(session_id, payload)] = api.posted
    assert session_id == "cse_1"
    assert payload["uuid"] == "mine"
    assert payload["session_id"] == "cse_1"
    assert "priority" not in payload, "the local CLI's queue slot means nothing there"


async def test_an_approval_is_asked_here_and_answered_there() -> None:
    api = FakeApi()
    client, options = _client(api)
    asked: list[tuple[str, str | None]] = []

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow:
        asked.append((name, context.tool_use_id))
        return PermissionResultAllow(updated_input={"command": "ls -la"})

    options.can_use_tool = can_use_tool
    await client.connect()
    request = {
        "type": "control_request",
        "request_id": "r1",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "input": {"command": "ls"},
            "tool_use_id": "t1",
        },
    }
    api.push(_event(7, request), control=True)
    api.push(_event(7, request), control=True)  # a reconnect replays it
    await eventually(lambda: bool(api.posted))
    await asyncio.sleep(0.02)
    assert asked == [("Bash", "t1")], "asked twice"
    [(_, answer)] = api.posted
    assert answer["type"] == "control_response"
    assert answer["response"]["request_id"] == "r1"
    assert answer["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": {"command": "ls -la"},
    }
    await client.disconnect()


async def test_an_approval_answered_elsewhere_is_withdrawn_here() -> None:
    api = FakeApi()
    client, options = _client(api)
    cancelled = asyncio.Event()

    async def can_use_tool(
        name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return PermissionResultAllow()

    options.can_use_tool = can_use_tool
    await client.connect()
    api.push(
        _event(
            7,
            {
                "type": "control_request",
                "request_id": "r1",
                "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}},
            },
        ),
        control=True,
    )
    await asyncio.sleep(0.02)
    api.push(
        _event(8, {"type": "control_response", "response": {"request_id": "r1"}}, "client"),
        control=True,
    )
    await asyncio.wait_for(cancelled.wait(), 1)
    assert api.posted == [], "answered an approval somebody else already had"
    await client.disconnect()


async def test_stop_is_an_interrupt_there() -> None:
    api = FakeApi()
    client, _ = _client(api)
    await client.interrupt()
    [(_, payload)] = api.posted
    assert payload["type"] == "control_request"
    assert payload["request"] == {"subtype": "interrupt"}


# -- the provider ------------------------------------------------------------------


class FakeDirectory:
    def __init__(self, provider: ClaudeProvider) -> None:
        self.provider = provider
        self.sessions: dict[str, ClaudeSession] = {}
        self.publishers: dict[str, FakePublisher] = {}
        self.titles: dict[str, str] = {}

    async def open(
        self,
        uri: str,
        *,
        title: str,
        resume_state: Mapping[str, Any],
        working_directories: Any = (),
    ) -> bool:
        if uri in self.sessions:
            return False
        publisher = FakePublisher()
        self.publishers[uri] = publisher
        self.titles[uri] = title
        self.sessions[uri] = await self.provider.resume_session(
            AgentSessionContext(
                session_uri=uri,
                chat_uri=f"{uri}/chat",
                provider_id="claude",
                resume_state=resume_state,
                publisher=publisher,
            )
        )
        return True

    async def close(self, uri: str) -> bool:
        session = self.sessions.pop(uri, None)
        if session is None:
            return False
        await session.disposed()
        await session.aclose()
        return True

    def uris(self) -> list[str]:
        return list(self.sessions)


def _row(remote_id: str, **over: str) -> RemoteSession:
    base = {
        "id": remote_id,
        "title": f"Session {remote_id}",
        "environment_kind": "bridge",
        "status": "active",
        "connection_status": "connected",
        "worker_status": "idle",
    }
    return RemoteSession.from_wire({**base, **over})


async def _provider(tmp_path: Path, api: FakeApi) -> tuple[ClaudeProvider, FakeDirectory]:
    provider = ClaudeProvider(tmp_path, claude_ai=api, state_dir=tmp_path, poll_s=3600)  # type: ignore[arg-type]
    directory = FakeDirectory(provider)
    await provider.attach_directory(directory)
    return provider, directory


async def test_the_accounts_live_sessions_are_listed(tmp_path: Path) -> None:
    api = FakeApi()
    api.rows = [
        _row("cse_live"),
        _row("cse_asleep", connection_status="disconnected"),
        _row("cse_archived", status="archived"),
        _row("cse_cloud", environment_kind="anthropic_cloud"),
    ]
    provider, directory = await _provider(tmp_path, api)
    await provider.sync_claude_ai()
    assert directory.uris() == [uri_of("cse_live")]
    assert directory.titles[uri_of("cse_live")] == "Session cse_live"
    state = await provider.resume_state_of(directory.sessions[uri_of("cse_live")])
    assert state == {"claudeAi": "cse_live", "permissionMode": "default"}
    await provider.aclose()


async def test_this_hosts_own_sessions_are_not_listed_twice(tmp_path: Path) -> None:
    api = FakeApi()
    api.rows = [_row("cse_mine")]
    provider, directory = await _provider(tmp_path, api)
    own = await provider.create_session(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude")
    )
    own.bridge_session_id = "cse_mine"
    await provider.sync_claude_ai()
    assert directory.uris() == []
    await provider.aclose()


async def test_titles_and_busy_follow_claude_ai(tmp_path: Path) -> None:
    api = FakeApi()
    api.rows = [_row("cse_1")]
    provider, directory = await _provider(tmp_path, api)
    await provider.sync_claude_ai()
    api.rows = [_row("cse_1", title="Renamed", worker_status="running")]
    publisher = directory.publishers[uri_of("cse_1")]
    titles: list[str] = []
    activities: list[str | None] = []

    async def title_changed(title: str) -> None:
        titles.append(title)

    async def activity_changed(activity: str | None) -> None:
        activities.append(activity)

    publisher.title_changed = title_changed  # type: ignore[method-assign]
    publisher.activity_changed = activity_changed  # type: ignore[method-assign]
    await provider.sync_claude_ai()
    assert titles == ["Renamed"]
    assert activities[-1] == "Working"
    await provider.aclose()


async def test_a_session_asleep_stays_listed_and_one_archived_goes(tmp_path: Path) -> None:
    api = FakeApi()
    api.rows = [_row("cse_1"), _row("cse_2")]
    provider, directory = await _provider(tmp_path, api)
    await provider.sync_claude_ai()
    api.rows = [_row("cse_1", connection_status="disconnected"), _row("cse_2", status="archived")]
    await provider.sync_claude_ai()
    assert directory.uris() == [uri_of("cse_1")]
    await provider.sync_claude_ai()
    api.rows.append(_row("cse_2"))  # unarchived there: it comes back
    await provider.sync_claude_ai()
    assert sorted(directory.uris()) == [uri_of("cse_1"), uri_of("cse_2")]
    await provider.aclose()


async def test_one_deleted_here_is_not_listed_again(tmp_path: Path) -> None:
    api = FakeApi()
    api.rows = [_row("cse_1")]
    provider, directory = await _provider(tmp_path, api)
    await provider.sync_claude_ai()
    await directory.close(uri_of("cse_1"))  # as a person deleting it would
    await provider.sync_claude_ai()
    assert directory.uris() == []
    await provider.aclose()

    again, directory = await _provider(tmp_path, api)
    await again.sync_claude_ai()
    assert directory.uris() == [], "forgotten across a restart"
    await again.aclose()


async def test_a_mirrored_session_takes_turns_through_claude_ai(tmp_path: Path) -> None:
    api = FakeApi()
    api.rows = [_row("cse_1")]
    provider, directory = await _provider(tmp_path, api)
    await provider.sync_claude_ai()
    session = directory.sessions[uri_of("cse_1")]
    await eventually(lambda: len(api.started_at) == 2)

    async def answer() -> None:
        await eventually(lambda: bool(api.posted))
        sent = api.posted[0][1]
        api.push(_typed(10, "hi", sent["uuid"]))  # our own message, echoed
        api.push(_assistant(11, "hello from there"))
        api.push(_result(12))

    task = asyncio.create_task(answer())
    sink = RecordingSink()
    await asyncio.wait_for(session.send_user_message(UserMessage(text="hi"), sink), 2)
    await task
    assert "".join(e[1] for e in sink.events if e[0] == "text") == "hello from there"
    assert directory.publishers[uri_of("cse_1")].turns == [], "our own message opened a turn"
    await provider.aclose()


async def test_a_turn_typed_there_opens_one_here(tmp_path: Path) -> None:
    """Typed in the desktop app or a terminal: Claude Code reports it with
    `origin: human`. From a claude.ai app it has none, and gets one."""
    api = FakeApi()
    api.rows = [_row("cse_1")]
    provider, directory = await _provider(tmp_path, api)
    await provider.sync_claude_ai()
    await eventually(lambda: len(api.started_at) == 2)
    typed = _typed(10, "from the terminal", "t-1", source="worker")
    api.push(Event(10, "worker", {**typed.payload, "origin": {"kind": "human"}}))
    api.push(_assistant(11, "sure"))
    api.push(_result(12))
    publisher = directory.publishers[uri_of("cse_1")]
    await eventually(lambda: bool(publisher.tasks) and publisher.tasks[0].done())
    [(text, sink)] = publisher.turns
    assert text == "from the terminal"
    assert "".join(e[1] for e in sink.events if e[0] == "text") == "sure"
    await provider.aclose()
    assert api.closed
