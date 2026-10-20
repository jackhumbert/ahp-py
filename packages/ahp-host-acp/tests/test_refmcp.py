"""The reference agent's stdio MCP client against a real fake server subprocess."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from ahp_host_acp.reference.mcp import (
    McpCancelled,
    McpClient,
    McpError,
    child_env,
    from_acp_entry,
)

SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))


def _client(env: dict[str, str] | None = None) -> McpClient:
    client = McpClient("fake", sys.executable, [SERVER], env or {})
    client.start(timeout=10.0)
    return client


def test_refmcp_lists_and_calls() -> None:
    client = _client()
    try:
        assert [t["name"] for t in client.list_tools()] == ["echo", "fail"]
        assert client.call_tool("echo", {"text": "hi"}) == {
            "content": [{"type": "text", "text": "hi"}],
            "isError": False,
        }
        assert client.call_tool("fail", {})["isError"] is True
        proc = client._proc
        assert proc is not None
    finally:
        client.close()
    assert proc.poll() is not None
    client.close()


def test_refmcp_follows_cursor() -> None:
    client = _client({"FAKE_MCP_PAGED": "1"})
    try:
        assert [t["name"] for t in client.list_tools()] == ["echo", "fail"]
    finally:
        client.close()


def test_refmcp_error_raises() -> None:
    client = _client()
    try:
        with pytest.raises(McpError, match="unknown tool"):
            client.call_tool("nope", {})
    finally:
        client.close()


def test_refmcp_from_acp_entry() -> None:
    entry = {
        "name": "t",
        "command": sys.executable,
        "args": [SERVER],
        "env": [{"name": "A", "value": "1"}],
    }
    assert isinstance(from_acp_entry(entry), McpClient)
    http = {"type": "http", "name": "h", "url": "http://example.com", "headers": []}
    assert from_acp_entry(http) is None


def test_refmcp_exit_fails_waiters() -> None:
    client = _client()
    try:
        proc = client._proc
        assert proc is not None
        proc.kill()
        proc.wait()
        started = time.monotonic()
        with pytest.raises(McpError):
            client.call_tool("echo", {"text": "hi"})
        assert time.monotonic() - started < 5.0
    finally:
        client.close()


def _text(result: dict[str, object]) -> str:
    content = result["content"]
    assert isinstance(content, list)
    return str(content[0]["text"])


def test_refmcp_child_env_drops_private_and_secret_names() -> None:
    parent = {
        "PATH": "/bin",
        "HOME": "/Users/me",
        "AHP_OPENAI_API_KEY": "sk-x",
        "AHP_OPENAI_BASE_URL": "http://example.com/v1",
        "GITHUB_TOKEN": "t",
        "my_secret": "s",
        "DB_PASSWORD": "p",
        "SOME_API_KEY": "k",
    }
    explicit = {"RELAY_TOKEN": "kept", "EXTRA": "1"}
    assert child_env(parent, explicit) == {
        "PATH": "/bin",
        "HOME": "/Users/me",
        "RELAY_TOKEN": "kept",
        "EXTRA": "1",
    }


def test_refmcp_server_does_not_see_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AHP_OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("AHP_OPENAI_MODEL", "m")
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "t")
    monkeypatch.setenv("PLAIN_SETTING", "plain")
    client = _client({"FAKE_MCP_EXTRA": "1", "ENTRY_TOKEN": "given"})
    try:
        seen = {
            name: _text(client.call_tool("env", {"name": name}))
            for name in (
                "AHP_OPENAI_API_KEY",
                "AHP_OPENAI_MODEL",
                "SOME_SERVICE_TOKEN",
                "PLAIN_SETTING",
                "ENTRY_TOKEN",
            )
        }
    finally:
        client.close()
    assert seen == {
        "AHP_OPENAI_API_KEY": "<unset>",
        "AHP_OPENAI_MODEL": "<unset>",
        "SOME_SERVICE_TOKEN": "<unset>",
        "PLAIN_SETTING": "plain",
        "ENTRY_TOKEN": "given",
    }


def test_refmcp_cancel_returns_promptly() -> None:
    client = _client({"FAKE_MCP_EXTRA": "1"})
    cancel = threading.Event()
    try:
        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        with pytest.raises(McpCancelled):
            client.call_tool("hang", {}, cancel=cancel)
        assert time.monotonic() - started < 3.0
        # The client is still usable after a call it gave up on.
        assert _text(client.call_tool("echo", {"text": "after"})) == "after"
    finally:
        client.close()


def test_refmcp_call_timeout() -> None:
    client = _client({"FAKE_MCP_EXTRA": "1"})
    try:
        with pytest.raises(McpError, match="timed out"):
            client.call_tool("hang", {}, cancel=threading.Event(), timeout=0.2)
    finally:
        client.close()
