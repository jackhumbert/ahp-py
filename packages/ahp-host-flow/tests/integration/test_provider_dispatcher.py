"""`ProviderDispatcher` over a real in-memory host: each `agent()` call as a worker chat.

A test-only outer provider (`HarnessProvider`) runs one `agent()` call per message, so the
dispatcher is exercised exactly as the flow provider will use it, with inner providers loaded by
import path from flow.toml data.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_host import (
    AgentInfo,
    AgentProvider,
    AgentSessionContext,
    Host,
    LoopbackSingleUserPolicy,
    TurnSink,
    UserMessage,
)

from ahp_host_flow.bridge import ScopedPublisher
from ahp_host_flow.config import FlowConfig, parse_config
from ahp_host_flow.dispatch import ProviderDispatcher
from ahp_host_flow.engine import AgentCall
from ahp_host_flow.loading import LazyProviders, load_providers
from tests.flow_fakes import HangingProvider, RecordingProvider, reset_fakes
from tests.hosting import (
    Wire,
    cancel_turn,
    connect,
    flow_tasks,
    found,
    open_session,
    run_turn,
    shut,
    start_turn,
    state,
    text_of,
    wait_turn,
    worker_chats,
)

SESSION = "flow-dispatch:/run-1"
ECHO = "tests.flow_fakes:StrippedEchoProvider"
REC = "tests.flow_fakes:RecordingProvider"
HANG = "tests.flow_fakes:HangingProvider"
SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


def make_config(
    base: Path,
    *,
    default: str = "echo",
    rules: list[dict[str, str]] | None = None,
    rec: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> FlowConfig:
    return parse_config(
        {
            "providers": {
                **(extra or {}),
                "echo": {"load": ECHO, "config": {}},
                "echo_s": {"load": ECHO, "config": {"structured": True}},
                "echo_c": {"load": ECHO, "config": {"confirm_tools": True}},
                "rec": {"load": REC, **(rec or {})},
                "hang": {"load": HANG},
            },
            "routing": {"default": default, "rules": rules or []},
        },
        base=base,
    )


class HarnessSession:
    def __init__(self, provider: HarnessProvider, context: AgentSessionContext) -> None:
        self.provider = provider
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        request = json.loads(message.text)
        assert self.context.publisher is not None
        assert message.chat_uri is not None
        dispatcher = ProviderDispatcher(
            config=self.provider.config,
            providers=self.provider.providers(),
            sink=sink,
            publisher=self.context.publisher,
            chat=message.chat_uri,
            session_uri=self.context.session_uri,
        )
        call = AgentCall(
            0,
            request["prompt"],
            request.get("opts", {}),
            request.get("phase"),
            request.get("label"),
        )
        task = asyncio.create_task(dispatcher.run_agent(call))
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.wait({task})
            raise
        if task.cancelled():
            reply: dict[str, Any] = {"skipped": True}
        elif (exc := task.exception()) is not None:
            reply = {"error": str(exc)}
        else:
            result = task.result()
            reply = {"value": result.value, "usage": dict(result.usage)}
        await sink.text_delta(json.dumps(reply))

    async def cancel(self, reason: str | None = None) -> None:
        return None

    async def aclose(self) -> None:
        return None


class HarnessProvider:
    agent = AgentInfo(
        provider="harness", display_name="Harness", description="One agent() call per message."
    )

    def __init__(self, config: FlowConfig, *, lazy: bool = False) -> None:
        self.config = config
        self.lazy = lazy

    def providers(self) -> Mapping[str, AgentProvider]:
        return LazyProviders(self.config) if self.lazy else load_providers(self.config)

    async def create_session(self, context: AgentSessionContext) -> HarnessSession:
        return HarnessSession(self, context)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


async def boot(
    config: FlowConfig, *, lazy: bool = False
) -> tuple[Host, Wire, asyncio.Task[None], str]:
    host = Host(HarnessProvider(config, lazy=lazy), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    chat = await open_session(wire, SESSION, "harness")
    return host, wire, serving, chat


def request(prompt: str, **extra: Any) -> str:
    return json.dumps({"prompt": prompt, **extra})


async def ask(wire: Wire, chat: str, text: str, turn_id: str = "t1") -> dict[str, Any]:
    turn = await run_turn(wire, chat, turn_id, text)
    reply = json.loads(text_of(turn))
    assert isinstance(reply, dict)
    return reply


async def subscribe_worker(wire: Wire) -> str:
    """Wait for the one worker chat and subscribe to it; returns its channel."""
    assert await wire.until(lambda: len(worker_chats(wire.host, SESSION)) == 1)
    worker = str(worker_chats(wire.host, SESSION)[0]["resource"])
    await wire.request("subscribe", {"channel": worker})
    return worker


async def test_title_and_link(tmp_path: Path) -> None:
    host, wire, serving, default = await boot(make_config(tmp_path))
    try:
        turn = await run_turn(wire, default, "t1", request("hello there", label="greet"))
        assert json.loads(text_of(turn))["value"] == "You said: hello there"
        (chat,) = worker_chats(host, SESSION)
        assert chat["title"] == "greet"
        assert chat["origin"] == {"kind": "tool", "chat": default, "toolCallId": "agent-0"}
        (subagent,) = found(turn, lambda m: m.get("type") == "subagent")
        assert subagent["resource"] == chat["resource"]

        long_prompt = "x" * 80
        reply = await ask(wire, default, request(long_prompt), "t2")
        assert reply["value"] == f"You said: {long_prompt}"
        titles = [c["title"] for c in worker_chats(host, SESSION)]
        assert titles[-1] == "x" * 59 + "…"
    finally:
        await shut(host, wire, serving)


async def test_routing(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        rules=[{"provider": "rec", "label": "rec*"}, {"provider": "rec", "phase": "Review"}],
    )
    host, wire, serving, default = await boot(config)
    try:
        assert (await ask(wire, default, request("a", label="rec-1"), "t1"))["value"] == "rec: a"
        assert (await ask(wire, default, request("b", phase="Review"), "t2"))["value"] == "rec: b"
        assert (await ask(wire, default, request("c", label="other"), "t3"))["value"] == (
            "You said: c"
        )
        # The harness loads fresh providers per message, so there is one RecordingProvider
        # instance per call (three), the third unused: count sessions, not instances.
        assert [len(p.sessions) for p in RecordingProvider.instances] == [1, 1, 0]
    finally:
        await shut(host, wire, serving)


async def test_config_reaches_inner_session(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        default="rec",
        rec={"models": {"big": "rec-large"}, "approval_mode": "acceptEdits", "effort": "low"},
    )
    host, wire, serving, default = await boot(config)
    try:
        reply = await ask(wire, default, request("hi", opts={"model": "big", "effort": "high"}))
        # The usage carries cache reads since M16 item 2 (input/cache budgets).
        assert reply["usage"] == {"input_tokens": 1, "output_tokens": 5, "cache_read_tokens": 0}
        session = RecordingProvider.instances[0].sessions[0]
        assert session.messages[0].model is not None
        assert session.messages[0].model.id == "rec-large"
        assert session.context.config == {"permissionMode": "acceptEdits", "effort": "high"}
        (chat,) = worker_chats(host, SESSION)
        assert session.context.chat_uri == chat["resource"]
        assert isinstance(session.context.publisher, ScopedPublisher)
        assert session.closed

        await ask(wire, default, request("again", opts={}), "t2")
        # the second call's providers are a fresh load (instances[1]), see test_routing
        again = RecordingProvider.instances[1].sessions[0]
        assert again.context.config == {"permissionMode": "acceptEdits", "effort": "low"}
        assert again.messages[0].model is None
    finally:
        await shut(host, wire, serving)


async def test_structured_success(tmp_path: Path) -> None:
    host, wire, serving, default = await boot(make_config(tmp_path, default="echo_s"))
    try:
        reply = await ask(wire, default, request('{"n": 3}', opts={"schema": SCHEMA}))
        assert reply["value"] == {"n": 3}
    finally:
        await shut(host, wire, serving)


async def test_structured_failure(tmp_path: Path) -> None:
    host, wire, serving, default = await boot(make_config(tmp_path, default="echo_s"))
    try:
        reply = await ask(wire, default, request('{"n": "three"}', opts={"schema": SCHEMA}))
        assert reply["error"].startswith("No valid structured result after 3 attempt(s)")
        (chat,) = worker_chats(host, SESSION)
        worker = str(chat["resource"])
        assert await wire.until(lambda: len(state(host, worker).get("turns", [])) == 1)
        assert state(host, worker)["turns"][0]["state"] == "error"
    finally:
        await shut(host, wire, serving)


async def test_skip(tmp_path: Path) -> None:
    host, wire, serving, default = await boot(make_config(tmp_path, default="hang"))
    try:
        await start_turn(wire, default, "t1", request("wait"))
        worker = await subscribe_worker(wire)

        def running() -> bool:
            if not HangingProvider.instances or not HangingProvider.instances[0].sessions:
                return False
            return (
                HangingProvider.instances[0].sessions[0].started.is_set()
                and state(host, worker).get("activeTurn") is not None
            )

        assert await wire.until(running)
        await cancel_turn(wire, worker, state(host, worker)["activeTurn"]["id"])
        turn = await wait_turn(wire, default, "t1")
        assert json.loads(text_of(turn)) == {"skipped": True}
        assert HangingProvider.instances[0].sessions[0].cancelled == ["skipped"]
        assert await wire.until(lambda: HangingProvider.instances[0].sessions[0].closed)
        assert await wire.until(lambda: not flow_tasks())
        (done,) = found(turn, lambda m: m.get("pastTenseMessage") == "agent() skipped")
        assert done["success"] is False
    finally:
        await shut(host, wire, serving)


async def test_abort(tmp_path: Path) -> None:
    host, wire, serving, default = await boot(make_config(tmp_path, default="hang"))
    try:
        await start_turn(wire, default, "t1", request("wait"))
        worker = await subscribe_worker(wire)

        def running() -> bool:
            if not HangingProvider.instances or not HangingProvider.instances[0].sessions:
                return False
            return HangingProvider.instances[0].sessions[0].started.is_set()

        assert await wire.until(running)
        await cancel_turn(wire, default, "t1")
        turn = await wait_turn(wire, default, "t1")
        assert turn["state"] == "cancelled"
        session = HangingProvider.instances[0].sessions[0]
        assert await wire.until(lambda: session.cancelled == ["run aborted"])
        assert await wire.until(lambda: state(host, worker).get("activeTurn") is None)
        assert await wire.until(lambda: session.closed)
        assert await wire.until(lambda: not flow_tasks())
    finally:
        await shut(host, wire, serving)


async def test_approval_from_worker_chat(tmp_path: Path) -> None:
    host, wire, serving, default = await boot(make_config(tmp_path, default="echo_c"))
    try:
        await start_turn(wire, default, "t1", request("approve me"))
        worker = await subscribe_worker(wire)

        def pending() -> list[Mapping[str, Any]]:
            active = state(host, worker).get("activeTurn") or {}
            return found(active, lambda m: m.get("status") == "pending-confirmation")

        assert await wire.until(lambda: bool(pending()))
        call = pending()[0]
        await wire.dispatch(
            worker,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": state(host, worker)["activeTurn"]["id"],
                "toolCallId": call.get("toolCallId", "echo-tool-1"),
                "approved": True,
                "confirmed": "user-action",
            },
        )
        turn = await wait_turn(wire, default, "t1")
        assert json.loads(text_of(turn))["value"] == "You said: approve me"
    finally:
        await shut(host, wire, serving)


BROKEN_SESSION = {"load": "tests.flow_fakes:BrokenSessionProvider"}


async def test_a_failing_create_session_completes_the_tool_call(tmp_path: Path) -> None:
    config = make_config(tmp_path, default="broken", extra={"broken": BROKEN_SESSION})
    host, wire, serving, default = await boot(config)
    try:
        turn = await run_turn(wire, default, "t1", request("hi"))
        assert "no session for you" in json.loads(text_of(turn))["error"]
        (done,) = found(turn, lambda m: m.get("pastTenseMessage") == "agent() failed")
        assert done["success"] is False
        assert not found(turn, lambda m: m.get("status") == "running")
    finally:
        await shut(host, wire, serving)


async def test_a_worker_turn_cancelled_before_it_starts_is_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Host._start_turn

    async def cancelling(self: Host, session: Any, channel: str, *args: Any, **kwargs: Any) -> None:
        await original(self, session, channel, *args, **kwargs)
        if channel in session.provider_chats:
            # The host (or a client) cancels the worker turn before it takes its first step.
            session.turns[channel].cancel()

    monkeypatch.setattr(Host, "_start_turn", cancelling)
    host, wire, serving, default = await boot(make_config(tmp_path, default="rec"))
    try:
        await start_turn(wire, default, "t1", request("hi"))
        turn = await asyncio.wait_for(wait_turn(wire, default, "t1"), timeout=5)
        assert json.loads(text_of(turn)) == {"skipped": True}
        (done,) = found(turn, lambda m: m.get("pastTenseMessage") == "agent() skipped")
        assert done["success"] is False
        (session,) = RecordingProvider.instances[0].sessions
        assert session.closed
        assert session.cancelled == ["skipped"]
        assert not session.messages
    finally:
        await shut(host, wire, serving)


async def test_a_broken_provider_fails_only_its_own_calls(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = make_config(
        tmp_path,
        rules=[{"provider": "gone", "label": "gone*"}],
        extra={"gone": {"load": "tests.flow_nope:Missing"}},
    )
    host, wire, serving, default = await boot(config, lazy=True)
    try:
        assert (await ask(wire, default, request("a")))["value"] == "You said: a"
        with caplog.at_level("WARNING"):
            reply = await ask(wire, default, request("b", label="gone-1"), "t2")
        assert "provider 'gone' is unavailable" in reply["error"]
        assert any("provider 'gone' failed to load" in r.getMessage() for r in caplog.records)
        assert (await ask(wire, default, request("c"), "t3"))["value"] == "You said: c"
    finally:
        await shut(host, wire, serving)
