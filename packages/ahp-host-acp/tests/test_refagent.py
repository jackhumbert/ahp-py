"""The reference ACP agent as a real subprocess, driven by `ahp_acp.AcpClient` against the stub."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
import threading
import tomllib
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ahp_acp
import pytest
from ahp_acp import AcpClient, AcpError, SessionUpdate

from ahp_host_acp.reference import command
from ahp_host_acp.reference.agent import Agent, from_env
from ahp_host_acp.reference.stub import Reply, StubServer, call, refuse, say

KEY = "sk-test-secret"
SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))
WAIT = 10.0

Answer = Callable[[Sequence[Mapping[str, Any]]], dict[str, Any]]


@dataclass
class Run:
    client: AcpClient
    updates: list[SessionUpdate] = field(default_factory=list)
    permissions: list[dict[str, Any]] = field(default_factory=list)
    requests: list[tuple[str, Any]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    chunk_seen: asyncio.Event = field(default_factory=asyncio.Event)


@contextlib.asynccontextmanager
async def _agent(
    stub: StubServer,
    *,
    answer: Answer = ahp_acp.allow_once,
    capabilities: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    extra_env: dict[str, str] | None = None,
) -> AsyncIterator[Run]:
    run: Run | None = None

    async def on_update(update: SessionUpdate) -> None:
        assert run is not None
        run.updates.append(update)
        if isinstance(update, ahp_acp.AgentMessageChunk) and update.content["text"] == "Hel":
            run.chunk_seen.set()

    async def on_permission(params: dict[str, Any]) -> dict[str, Any]:
        assert run is not None
        run.permissions.append(params)
        return answer(params["options"])

    async def on_request(method: str, params: Any) -> Any:
        assert run is not None
        run.requests.append((method, params))
        return {}

    lines: list[str] = []
    env = stub.agent_env(api_key=KEY, **(extra or {})) | (extra_env or {})
    client = AcpClient(
        command(),
        spawn_options={"env": env, "on_stderr": lines.append},
        client_info={"name": "test", "version": "0"},
        client_capabilities=capabilities,
        on_update=on_update,
        on_permission=on_permission,
        on_request=on_request,
    )
    run = Run(client, lines=lines)
    async with client:
        await asyncio.wait_for(client.initialize(), WAIT)
        yield run


async def _session(run: Run, cwd: Path, servers: Sequence[Mapping[str, Any]] = ()) -> str:
    result = await asyncio.wait_for(run.client.new_session(cwd, servers), WAIT)
    return str(result["sessionId"])


async def _prompt(run: Run, sid: str, text: str = "go") -> dict[str, Any]:
    return await asyncio.wait_for(run.client.prompt(sid, [{"type": "text", "text": text}]), WAIT)


def _echo_server() -> dict[str, Any]:
    return ahp_acp.mcp_server_stdio("tools", sys.executable, [SERVER])


def _tool_message(stub: StubServer, index: int) -> dict[str, Any]:
    message: dict[str, Any] = stub.requests[index]["messages"][-1]
    return message


async def test_refagent_initialize() -> None:
    with StubServer([]) as stub:
        async with _agent(stub) as run:
            assert run.client.agent_capabilities["loadSession"] is False
            prompt = run.client.agent_capabilities["promptCapabilities"]
            assert prompt["embeddedContext"] is True
            assert run.client.agent_info is not None
            assert run.client.agent_info["name"] == "ahp-acp-openai"


def test_refagent_missing_env_exits() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AHP_OPENAI_")}
    done = subprocess.run(command(), env=env, input=b"", capture_output=True, timeout=60)
    assert done.returncode == 2
    assert "AHP_OPENAI_BASE_URL" in done.stderr.decode("utf-8", "replace")


async def test_refagent_text_reasoning_usage(tmp_path: Path) -> None:
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
        "prompt_tokens_details": {"cached_tokens": 2},
    }
    with StubServer([say("Hel", "lo", reasoning=("hm",), usage=usage)]) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, tmp_path)
            result = await _prompt(run, sid)
        thoughts = [u for u in run.updates if isinstance(u, ahp_acp.AgentThoughtChunk)]
        texts = [u for u in run.updates if isinstance(u, ahp_acp.AgentMessageChunk)]
        assert [u.content["text"] for u in thoughts] == ["hm"]
        assert [u.content["text"] for u in texts] == ["Hel", "lo"]
        assert result["stopReason"] == "end_turn"
        assert result["usage"] == {
            "inputTokens": 10,
            "outputTokens": 5,
            "cachedReadTokens": 2,
            "totalTokens": 17,
        }
        request = stub.requests[0]
        assert request["model"] == "stub-model"
        assert request["stream"] is True
        assert request["messages"][0]["role"] == "system"
        assert str(tmp_path) in request["messages"][0]["content"]
        assert request["messages"][-1] == {"role": "user", "content": "go"}
        assert [t["function"]["name"] for t in request["tools"]] == ["write_file"]


@pytest.mark.parametrize(("window", "size"), [("8192", 8192), (None, 0)])
async def test_refagent_reports_the_context_after_each_round(
    tmp_path: Path, window: str | None, size: int
) -> None:
    first = {"prompt_tokens": 40, "completion_tokens": 3, "total_tokens": 43}
    second = {"prompt_tokens": 60, "completion_tokens": 2, "total_tokens": 62}
    replies = [
        dataclasses.replace(
            call("write_file", {"path": "notes.txt", "content": "hello"}), usage=first
        ),
        say("done", usage=second),
    ]
    extra_env = {"AHP_OPENAI_CONTEXT_WINDOW": window} if window else None
    with StubServer(replies) as stub:
        async with _agent(stub, extra_env=extra_env) as run:
            sid = await _session(run, tmp_path)
            await _prompt(run, sid)
    usages = [u for u in run.updates if isinstance(u, ahp_acp.UsageUpdate)]
    # Each model call's prompt, as it was sent: the host reads the last as the gauge.
    assert [(u.used, u.size) for u in usages] == [(40, size), (60, size)]


def test_refagent_context_window_must_be_a_count() -> None:
    env = {"AHP_OPENAI_BASE_URL": "http://localhost:1/v1", "AHP_OPENAI_MODEL": "m"}
    assert from_env({**env, "AHP_OPENAI_CONTEXT_WINDOW": "4096"}).context_window == 4096
    for bad in ("0", "-1", "lots"):
        with pytest.raises(ValueError, match="AHP_OPENAI_CONTEXT_WINDOW"):
            from_env({**env, "AHP_OPENAI_CONTEXT_WINDOW": bad})


async def test_refagent_cancel_mid_stream(tmp_path: Path) -> None:
    with StubServer([Reply(deltas=({"content": "Hel"},), hang=True)]) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, tmp_path)
            turn = asyncio.ensure_future(_prompt(run, sid))
            await asyncio.wait_for(run.chunk_seen.wait(), WAIT)
            await run.client.cancel(sid)
            result = await asyncio.wait_for(turn, WAIT)
        assert result["stopReason"] == "cancelled"
        assert await asyncio.to_thread(stub.disconnected.wait, 5)


async def test_refagent_rate_limited_and_key_never_logged(tmp_path: Path) -> None:
    with StubServer([refuse(), say("ok")]) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, tmp_path)
            with pytest.raises(AcpError) as raised:
                await _prompt(run, sid)
            assert raised.value.code == -32603
            assert "rate limited" in raised.value.message
            assert "429" in raised.value.message
            assert raised.value.data == {"httpStatus": 429}
            await _prompt(run, sid, "again")
        assert not any(KEY in line for line in run.lines)
        assert KEY not in repr(run.updates)
        assert stub.headers[0]["authorization"] == f"Bearer {KEY}"
        users = [m for m in stub.requests[1]["messages"] if m["role"] == "user"]
        assert users == [{"role": "user", "content": "again"}]


async def test_refagent_write_allowed(tmp_path: Path) -> None:
    replies = [call("write_file", {"path": "notes.txt", "content": "hello"}), say("done")]
    with StubServer(replies) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, tmp_path)
            result = await _prompt(run, sid)
        target = tmp_path / "notes.txt"
        assert result["stopReason"] == "end_turn"
        assert target.read_text(encoding="utf-8") == "hello"
        tool_call = run.permissions[0]["toolCall"]
        assert tool_call["kind"] == "edit"
        assert tool_call["title"] == "Write notes.txt"
        assert tool_call["locations"] == [{"path": str(target)}]
        diff = tool_call["content"][0]
        assert diff["type"] == "diff"
        assert diff["newText"] == "hello"
        assert "oldText" not in diff
        finished = [u for u in run.updates if isinstance(u, ahp_acp.ToolCallUpdate)]
        assert "completed" in [u.status for u in finished]
        assert _tool_message(stub, 1) == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": f"Wrote 5 characters to {target}",
        }


async def test_refagent_write_rejected(tmp_path: Path) -> None:
    replies = [call("write_file", {"path": "notes.txt", "content": "hello"}), say("done")]
    with StubServer(replies) as stub:
        async with _agent(stub, answer=ahp_acp.reject_once) as run:
            sid = await _session(run, tmp_path)
            await _prompt(run, sid)
        assert not (tmp_path / "notes.txt").exists()
        finished = [u for u in run.updates if isinstance(u, ahp_acp.ToolCallUpdate)]
        assert "failed" in [u.status for u in finished]
        assert _tool_message(stub, 1)["content"] == "The user declined this call."


async def test_refagent_write_through_client(tmp_path: Path) -> None:
    replies = [call("write_file", {"path": "notes.txt", "content": "hello"}), say("done")]
    capabilities = {"fs": {"readTextFile": False, "writeTextFile": True}, "terminal": False}
    with StubServer(replies) as stub:
        async with _agent(stub, capabilities=capabilities) as run:
            sid = await _session(run, tmp_path)
            await _prompt(run, sid)
        assert not (tmp_path / "notes.txt").exists()
        assert len(run.requests) == 1
        method, params = run.requests[0]
        assert method == "fs/write_text_file"
        assert params["path"] == str(tmp_path / "notes.txt")
        assert params["content"] == "hello"


async def test_refagent_write_outside_folder_refused(tmp_path: Path) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    replies = [call("write_file", {"path": "../escape.txt", "content": "x"}), say("done")]
    with StubServer(replies) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, folder)
            await _prompt(run, sid)
        assert run.permissions == []
        assert not (tmp_path / "escape.txt").exists()
        calls = [u for u in run.updates if isinstance(u, ahp_acp.ToolCall)]
        assert [u.status for u in calls] == ["failed"]
        assert str(_tool_message(stub, 1)["content"]).startswith("Refused:")


async def test_refagent_mcp_tool(tmp_path: Path) -> None:
    replies = [call("mcp__tools__echo", {"text": "hi"}), say("done")]
    with StubServer(replies) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, tmp_path, [_echo_server()])
            await _prompt(run, sid)
        names = [t["function"]["name"] for t in stub.requests[0]["tools"]]
        assert "mcp__tools__echo" in names
        tool_call = run.permissions[0]["toolCall"]
        assert tool_call["kind"] == "other"
        assert tool_call["title"] == "mcp__tools__echo"
        assert tool_call["rawInput"] == {"text": "hi"}
        assert _tool_message(stub, 1)["content"] == "hi"


async def test_refagent_max_steps(tmp_path: Path) -> None:
    replies = [
        call("mcp__tools__echo", {"text": "hi"}),
        call("mcp__tools__echo", {"text": "hi"}, call_id="call_2"),
        say("x"),
    ]
    with StubServer(replies) as stub:
        async with _agent(stub, extra_env={"AHP_OPENAI_MAX_STEPS": "2"}) as run:
            sid = await _session(run, tmp_path, [_echo_server()])
            result = await _prompt(run, sid)
        assert result["stopReason"] == "max_turn_requests"
        assert len(stub.requests) == 2


async def test_refagent_finish_length(tmp_path: Path) -> None:
    with StubServer([Reply(deltas=({"content": "x"},), finish_reason="length")]) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, tmp_path)
            result = await _prompt(run, sid)
        assert result["stopReason"] == "max_tokens"


def test_refagent_console_script_declared() -> None:
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(path.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["ahp-acp-openai"] == "ahp_host_acp.reference.__main__:main"


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")


async def _write_refused(tmp_path: Path, path: str) -> Run:
    folder = tmp_path / "work"
    replies = [call("write_file", {"path": path, "content": "x"}), say("done")]
    with StubServer(replies) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, folder)
            await _prompt(run, sid)
        assert run.permissions == []
        calls = [u for u in run.updates if isinstance(u, ahp_acp.ToolCall)]
        assert [u.status for u in calls] == ["failed"]
        assert str(_tool_message(stub, 1)["content"]).startswith("Refused:")
    return run


async def test_refagent_write_through_symlinked_file_refused(tmp_path: Path) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    _symlink(folder / "innocent.txt", outside)
    await _write_refused(tmp_path, "innocent.txt")
    assert outside.read_text(encoding="utf-8") == "original"


async def test_refagent_write_through_symlinked_dir_refused(tmp_path: Path) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink(folder / "link", outside, directory=True)
    await _write_refused(tmp_path, "link/new.txt")
    assert not (outside / "new.txt").exists()


async def test_refagent_write_symlink_inside_shows_resolved_path(tmp_path: Path) -> None:
    folder = tmp_path / "work"
    (folder / "real").mkdir(parents=True)
    _symlink(folder / "alias", folder / "real", directory=True)
    replies = [call("write_file", {"path": "alias/notes.txt", "content": "hi"}), say("done")]
    with StubServer(replies) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, folder)
            await _prompt(run, sid)
    resolved = os.path.realpath(folder / "real" / "notes.txt")
    tool_call = run.permissions[0]["toolCall"]
    assert tool_call["locations"] == [{"path": resolved}]
    assert tool_call["content"][0]["path"] == resolved
    assert (folder / "real" / "notes.txt").read_text(encoding="utf-8") == "hi"


async def test_refagent_mcp_server_does_not_see_the_key(tmp_path: Path) -> None:
    replies = [call("mcp__tools__env", {"name": "AHP_OPENAI_API_KEY"}), say("done")]
    server = ahp_acp.mcp_server_stdio("tools", sys.executable, [SERVER], {"FAKE_MCP_EXTRA": "1"})
    with StubServer(replies) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, tmp_path, [server])
            await _prompt(run, sid)
        assert _tool_message(stub, 1)["content"] == "<unset>"


async def test_refagent_cancel_during_mcp_call(tmp_path: Path) -> None:
    replies = [call("mcp__tools__hang", {}), say("never")]
    server = ahp_acp.mcp_server_stdio("tools", sys.executable, [SERVER], {"FAKE_MCP_EXTRA": "1"})
    with StubServer(replies) as stub:
        async with _agent(stub) as run:
            sid = await _session(run, tmp_path, [server])
            turn = asyncio.ensure_future(_prompt(run, sid))

            def running() -> bool:
                return any(
                    isinstance(u, ahp_acp.ToolCallUpdate) and u.status == "in_progress"
                    for u in run.updates
                )

            for _ in range(int(WAIT / 0.02)):
                if running():
                    break
                await asyncio.sleep(0.02)
            assert running()
            await run.client.cancel(sid)
            result = await asyncio.wait_for(turn, 5.0)
        assert result["stopReason"] == "cancelled"
        assert len(stub.requests) == 1


class _Out:
    """A thread-safe stdout for an in-process Agent: collects JSON lines."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self.changed = threading.Condition()

    def write(self, data: bytes) -> int:
        with self.changed:
            for line in data.splitlines():
                self.lines.append(json.loads(line))
            self.changed.notify_all()
        return len(data)

    def flush(self) -> None:
        pass

    def reply(self, request_id: int) -> dict[str, Any]:
        def found() -> dict[str, Any] | None:
            return next((m for m in self.lines if m.get("id") == request_id), None)

        with self.changed:
            assert self.changed.wait_for(lambda: found() is not None, WAIT)
            answer = found()
            assert answer is not None
            return answer


def test_refagent_cancel_read_before_the_turn_starts(tmp_path: Path) -> None:
    # A cancel read right after its prompt, before the turn's thread has run, still cancels it.
    with StubServer([say("never")]) as stub:
        config = from_env(stub.agent_env())
        read_fd, write_fd = os.pipe()
        out = _Out()
        stdin = os.fdopen(read_fd, "rb")
        agent = Agent(config, stdin=stdin, stdout=out, stderr=io.StringIO())  # type: ignore[arg-type]
        server = threading.Thread(target=agent.serve, daemon=True)
        server.start()

        def send(*messages: dict[str, Any]) -> None:
            os.write(write_fd, b"".join(json.dumps(m).encode() + b"\n" for m in messages))

        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session/new",
                    "params": {"cwd": str(tmp_path), "mcpServers": []},
                }
            )
            sid = out.reply(1)["result"]["sessionId"]
            # One write: the reading thread reads both lines back to back.
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]},
                },
                {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}},
            )
            assert out.reply(2)["result"]["stopReason"] == "cancelled"
            assert stub.requests == []
        finally:
            os.close(write_fd)
            server.join(WAIT)
            stdin.close()
