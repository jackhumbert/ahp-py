"""The reference agent's chat-completions client against the scripted loopback stub."""

from __future__ import annotations

import ast
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

import ahp_host_acp.reference as reference
from ahp_host_acp.reference.chat import (
    Cancelled,
    ChatClient,
    Completion,
    EndpointError,
    ToolCallDraft,
    request_body,
)
from ahp_host_acp.reference.stub import Reply, StubServer, call, refuse, say

KEY = "sk-test-secret"
MESSAGES = [{"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]


def _run(
    stub: StubServer, messages: Any = MESSAGES, tools: Any = (), key: str | None = KEY
) -> tuple[Completion, list[str], list[str]]:
    texts: list[str] = []
    thoughts: list[str] = []
    client = ChatClient(stub.base_url, "stub-model", key, 10.0)
    done = client.stream(
        messages,
        tools,
        on_text=texts.append,
        on_reasoning=thoughts.append,
        cancelled=threading.Event(),
    )
    return done, texts, thoughts


def test_refchat_text_and_reasoning() -> None:
    with StubServer([say("Hel", "lo", reasoning=("hmm",))]) as stub:
        done, texts, thoughts = _run(stub)
    assert texts == ["Hel", "lo"]
    assert thoughts == ["hmm"]
    assert done.text == "Hello"
    assert done.finish_reason == "stop"
    assert done.tool_calls == []


def test_refchat_ollama_reasoning_key() -> None:
    with StubServer([Reply(deltas=({"reasoning": "r1"}, {"content": "x"}))]) as stub:
        done, _, _ = _run(stub)
    assert done.reasoning == "r1"
    assert done.text == "x"


def test_refchat_tool_call_accumulates() -> None:
    with StubServer([call("mcp__t__echo", {"text": "hi"}, call_id="c9")]) as stub:
        done, _, _ = _run(stub)
    assert done.tool_calls == [ToolCallDraft("c9", "mcp__t__echo", '{"text":"hi"}')]
    assert done.finish_reason == "tool_calls"


def test_refchat_usage() -> None:
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
        "prompt_tokens_details": {"cached_tokens": 2},
    }
    with StubServer([say("x", usage=usage)]) as stub:
        done, _, _ = _run(stub)
    assert done.usage == {"prompt": 12, "completion": 5, "cached": 2, "total": 17}


def test_refchat_request_shape() -> None:
    with StubServer([say("a"), say("b")]) as stub:
        _run(stub, tools=TOOLS)
        _run(stub, key=None)
    assert stub.requests[0] == request_body("stub-model", MESSAGES, TOOLS)
    assert stub.paths[0] == "/v1/chat/completions"
    assert stub.headers[0]["authorization"] == f"Bearer {KEY}"
    assert "authorization" not in stub.headers[1]
    assert "tools" not in stub.requests[1]


def test_refchat_rate_limited() -> None:
    with StubServer([refuse()]) as stub, pytest.raises(EndpointError) as caught:
        _run(stub)
    assert caught.value.status == 429
    for part in ("rate limited", "429", "Rate limit exceeded"):
        assert part in str(caught.value)


def test_refchat_key_redacted() -> None:
    with (
        StubServer([Reply(status=500, body=f"bad key {KEY}")]) as stub,
        pytest.raises(EndpointError) as caught,
    ):
        _run(stub)
    assert "***" in caught.value.message
    assert KEY not in caught.value.message


def test_refchat_unreachable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = ChatClient(f"http://127.0.0.1:{port}/v1", "stub-model", None, 5.0)
    with pytest.raises(EndpointError) as caught:
        client.stream(
            MESSAGES,
            (),
            on_text=lambda _: None,
            on_reasoning=lambda _: None,
            cancelled=threading.Event(),
        )
    assert caught.value.status is None


def test_refchat_abort_cancels() -> None:
    cancelled = threading.Event()
    first = threading.Event()
    outcome: list[BaseException] = []
    with StubServer([Reply(deltas=({"content": "a"},), hang=True)]) as stub:
        client = ChatClient(stub.base_url, "stub-model", None, 10.0)

        def work() -> None:
            try:
                client.stream(
                    MESSAGES,
                    (),
                    on_text=lambda _: first.set(),
                    on_reasoning=lambda _: None,
                    cancelled=cancelled,
                )
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        assert first.wait(10)
        cancelled.set()
        client.abort()
        thread.join(10)
        assert not thread.is_alive()
        assert stub.disconnected.wait(5)
    assert len(outcome) == 1
    assert isinstance(outcome[0], Cancelled)


def test_refchat_abort_during_connect_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    # An abort that lands while the socket is still being made (no socket to shut down yet) is
    # acted on as soon as the connection exists: nothing is sent.
    with StubServer([say("never")]) as stub:
        client = ChatClient(stub.base_url, "stub-model", None, 10.0)
        made = client._connect

        def connect_then_abort() -> Any:
            conn = made()
            real = conn.connect

            def connect() -> None:
                client.abort()
                real()

            conn.connect = connect  # type: ignore[method-assign]
            return conn

        monkeypatch.setattr(client, "_connect", connect_then_abort)
        with pytest.raises(Cancelled):
            client.stream(
                MESSAGES,
                (),
                on_text=lambda _: None,
                on_reasoning=lambda _: None,
                cancelled=threading.Event(),
            )
        assert stub.requests == []


def test_refchat_abort_during_send_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    with StubServer([say("never")]) as stub:
        client = ChatClient(stub.base_url, "stub-model", None, 10.0)
        made = client._connect

        def abort_on_request() -> Any:
            conn = made()
            real = conn.request

            def request(*args: Any, **kwargs: Any) -> None:
                client.abort()
                real(*args, **kwargs)

            conn.request = request  # type: ignore[method-assign]
            return conn

        monkeypatch.setattr(client, "_connect", abort_on_request)
        with pytest.raises(Cancelled):
            client.stream(
                MESSAGES,
                (),
                on_text=lambda _: None,
                on_reasoning=lambda _: None,
                cancelled=threading.Event(),
            )


def test_refchat_abort_is_cleared_for_the_next_request() -> None:
    with StubServer([say("ok")]) as stub:
        client = ChatClient(stub.base_url, "stub-model", None, 10.0)
        client.abort()
        done = client.stream(
            MESSAGES,
            (),
            on_text=lambda _: None,
            on_reasoning=lambda _: None,
            cancelled=threading.Event(),
        )
        assert done.text == "ok"


def test_refchat_cancelled_before_start_sends_nothing() -> None:
    cancelled = threading.Event()
    cancelled.set()
    with StubServer([say("never")]) as stub:
        client = ChatClient(stub.base_url, "stub-model", None, 10.0)
        with pytest.raises(Cancelled):
            client.stream(
                MESSAGES,
                (),
                on_text=lambda _: None,
                on_reasoning=lambda _: None,
                cancelled=cancelled,
            )
        assert stub.requests == []


def test_refchat_reference_is_stdlib_only() -> None:
    root = Path(reference.__file__).parent
    files = sorted(root.rglob("*.py"))
    assert files
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path.name}: relative import"
                names = [node.module or ""]
            for name in names:
                ok = name.split(".")[0] in sys.stdlib_module_names or name.startswith(
                    "ahp_host_acp.reference"
                )
                assert ok, f"{path.name} imports {name}"
