"""`[policy] host_tools`: fs and terminal served by the host, under the jail and the policy."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from ahp_acp import AcpError
from ahp_host.provider.base import UserMessage
from ahp_host.provider.policy import ToolPolicy

from ahp_host_acp.agent import provider_from_options
from ahp_host_acp.client_methods import OUTPUT_LIMIT, ClientMethods, _Terminal
from ahp_host_acp.config import ConfigError
from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec
from ahp_host_acp.roots import Roots

from .fakes import FAKE_AGENT, RecordingSink, context

POLICY = ToolPolicy(deny_paths=("**/.env",), deny_commands=("git push*",), ask_paths=("**/ask/*",))


async def _session(tmp_path: Path, *, client_methods: bool = True) -> tuple[AcpSession, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = AgentSpec(FAKE_AGENT, env={}, policy=POLICY, client_methods=client_methods)
    return await AcpProvider(ws, spec).create_session(context(ws)), ws


async def _say(session: AcpSession, text: str, sink: RecordingSink | None = None) -> str:
    sink = sink or RecordingSink()
    await session.send_user_message(UserMessage(text=text), sink)
    return "".join(str(e[1]) for e in sink.events if e[0] == "text")


async def calls(
    session: AcpSession, items: list[dict[str, Any]], sink: RecordingSink | None = None
) -> list[Any]:
    got: list[Any] = json.loads(await _say(session, "clients " + json.dumps(items), sink))
    return got


async def test_acphostfs_capabilities(tmp_path: Path) -> None:
    session, _ = await _session(tmp_path)
    try:
        assert json.loads(await _say(session, "caps")) == {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
        }
    finally:
        await session.aclose()
    plain = tmp_path / "plain"
    plain.mkdir()
    off = await AcpProvider(plain, AgentSpec(FAKE_AGENT, env={})).create_session(context(plain))
    try:
        assert json.loads(await _say(off, "caps")) == {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        }
    finally:
        await off.aclose()


async def test_acphostfs_read(tmp_path: Path) -> None:
    session, ws = await _session(tmp_path)
    (ws / "notes.txt").write_bytes(b"one\ntwo\n")
    (ws / ".env").write_bytes(b"SECRET=1\n")
    (tmp_path / "outside.txt").write_bytes(b"no\n")
    read = "fs/read_text_file"
    try:
        got = await calls(
            session,
            [
                {"method": read, "params": {"path": str(ws / "notes.txt")}},
                {"method": read, "params": {"path": str(ws / "notes.txt"), "line": 2, "limit": 1}},
                {"method": read, "params": {"path": str(ws / ".env")}},
                {"method": read, "params": {"path": str(tmp_path / "outside.txt")}},
                {"method": read, "params": {"path": str(ws / "missing.txt")}},
            ],
        )
    finally:
        await session.aclose()
    assert got[0] == {"content": "one\ntwo\n"}
    assert got[1] == {"content": "two\n"}
    assert got[2]["error"]["code"] == -32009
    assert "'**/.env'" in got[2]["error"]["message"]
    assert got[3]["error"]["code"] == -32009
    assert "outside this host's folders" in got[3]["error"]["message"]
    assert got[4]["error"]["code"] == -32002


async def test_acphostfs_write(tmp_path: Path) -> None:
    session, ws = await _session(tmp_path)
    write = "fs/write_text_file"
    try:
        got = await calls(
            session,
            [
                {"method": write, "params": {"path": str(ws / "out" / "new.txt"), "content": "hi"}},
                {"method": write, "params": {"path": str(ws / ".env"), "content": "x"}},
            ],
        )
    finally:
        await session.aclose()
    assert got[0] == {}
    assert (ws / "out" / "new.txt").read_bytes() == b"hi"
    assert got[1]["error"]["code"] == -32009
    assert not (ws / ".env").exists()


async def test_acphostfs_ask_path(tmp_path: Path) -> None:
    session, ws = await _session(tmp_path)
    (ws / "ask").mkdir()
    (ws / "ask" / "a").write_bytes(b"secret")
    item = {"method": "fs/read_text_file", "params": {"path": str(ws / "ask" / "a")}}
    try:
        approved = RecordingSink()
        assert await calls(session, [item], approved) == [{"content": "secret"}]
        assert len(approved.confirmations) == 1
        got = await calls(session, [item], RecordingSink(approve=False))
    finally:
        await session.aclose()
    assert got[0]["error"]["code"] == -32009
    assert got[0]["error"]["message"].startswith("declined:")


async def test_acphostfs_terminal_runs(tmp_path: Path) -> None:
    session, _ = await _session(tmp_path)
    terminal = {"terminalId": "$terminal"}
    try:
        got = await calls(
            session,
            [
                {
                    "method": "terminal/create",
                    "params": {"command": sys.executable, "args": ["-c", "print('hi')"]},
                },
                {"method": "terminal/wait_for_exit", "params": terminal},
                {"method": "terminal/output", "params": terminal},
                {"method": "terminal/release", "params": terminal},
            ],
        )
    finally:
        await session.aclose()
    assert got[1]["exitCode"] == 0
    assert got[2]["output"].strip() == "hi"
    assert got[2]["truncated"] is False
    assert got[3] == {}


async def test_acphostfs_terminal_push_refused(tmp_path: Path) -> None:
    session, _ = await _session(tmp_path)
    create = "terminal/create"
    try:
        got = await calls(
            session,
            [
                {"method": create, "params": {"command": "git", "args": ["push", "origin"]}},
                {"method": create, "params": {"command": "bash", "args": ["-c", "git push"]}},
                {"method": create, "params": {"command": "echo", "cwd": str(tmp_path)}},
            ],
        )
    finally:
        await session.aclose()
    for refused in got:
        assert refused["error"]["code"] == -32009
    assert "'git push*'" in got[0]["error"]["message"]
    assert "'git push*'" in got[1]["error"]["message"]
    assert "outside" in got[2]["error"]["message"]


async def test_acphostfs_output_limit(tmp_path: Path) -> None:
    session, _ = await _session(tmp_path)
    terminal = {"terminalId": "$terminal"}
    try:
        got = await calls(
            session,
            [
                {
                    "method": "terminal/create",
                    "params": {
                        "command": sys.executable,
                        "args": ["-c", "print('x' * 5000)"],
                        "outputByteLimit": 100,
                    },
                },
                {"method": "terminal/wait_for_exit", "params": terminal},
                {"method": "terminal/output", "params": terminal},
                {"method": "terminal/release", "params": terminal},
            ],
        )
    finally:
        await session.aclose()
    assert len(got[2]["output"]) <= 100
    assert got[2]["truncated"] is True
    assert set(got[2]["output"].strip()) == {"x"}


def test_acphostfs_config(tmp_path: Path) -> None:
    roots, state = Roots.single(tmp_path), tmp_path
    provider = provider_from_options(
        {"command": ["x"], "policy": {"host_tools": True, "deny_paths": ["**/.env"]}},
        roots,
        state,
    )
    assert provider.spec.client_methods is True
    assert provider.spec.policy is not None
    assert provider.spec.policy.deny_paths == ("**/.env",)
    with pytest.raises(ConfigError, match="host_tools"):
        provider_from_options({"command": ["x"], "policy": {"host_tools": "yes"}}, roots, state)


# -- ClientMethods directly ------------------------------------------------------


def _methods(ws: Path, **fields: Any) -> ClientMethods:
    fields.setdefault("policy", POLICY)
    fields.setdefault("env_allow", None)
    return ClientMethods(roots=Roots.single(ws), cwd=lambda: ws, **fields)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0 until it is reaped.
    with contextlib.suppress(OSError):
        if Path(f"/proc/{pid}/stat").read_text().split(") ")[-1].startswith("Z"):
            return False
    return True


GRANDCHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)


@pytest.mark.parametrize("method", ["terminal/release", "terminal/kill"])
async def test_acphostfs_terminal_kills_its_grandchildren(tmp_path: Path, method: str) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    methods = _methods(ws, policy=None)
    created = await methods.handle(
        "terminal/create", {"command": sys.executable, "args": ["-c", GRANDCHILD]}, None
    )
    terminal = {"terminalId": created["terminalId"]}
    pid = 0
    try:
        for _ in range(200):
            output = await methods.handle("terminal/output", terminal, None)
            if output["output"].strip():
                pid = int(output["output"].split()[0])
                break
            await asyncio.sleep(0.05)
        assert pid
        assert _alive(pid)
        await methods.handle(method, terminal, None)
        for _ in range(100):
            if not _alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not _alive(pid)
    finally:
        await methods.aclose()


#: A command whose child leaves its process group and keeps the output pipe open
#: for a while, so a kill of the group does not end the terminal's reader.
ESCAPED = (
    "import subprocess, sys, time\n"
    "subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(4)'], start_new_session=True\n"
    ")\n"
    "print('ready', flush=True)\n"
    "time.sleep(120)\n"
)


async def test_acphostfs_wait_for_exit_answers_through_a_reap(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    methods = _methods(ws, policy=None)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", stdout=asyncio.subprocess.PIPE
    )
    await process.wait()
    terminal = _Terminal(process, OUTPUT_LIMIT)
    terminal.reader = asyncio.create_task(asyncio.sleep(60))  # output still being read
    methods._terminals["term-x"] = terminal
    waiting = asyncio.ensure_future(
        methods.handle("terminal/wait_for_exit", {"terminalId": "term-x"}, None)
    )
    await asyncio.sleep(0.05)
    # A release (or the session's end) reaps the terminal, cancelling its reader:
    # the agent's wait is still answered, not left without a reply.
    await ClientMethods._reap(terminal)
    assert await asyncio.wait_for(waiting, 5) == {"exitCode": 0, "signal": None}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sessions")
async def test_acphostfs_release_is_not_held_by_an_escaped_child(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    methods = _methods(ws, policy=None)
    created = await methods.handle(
        "terminal/create", {"command": sys.executable, "args": ["-c", ESCAPED]}, None
    )
    terminal = {"terminalId": created["terminalId"]}
    try:
        for _ in range(200):
            if "ready" in (await methods.handle("terminal/output", terminal, None))["output"]:
                break
            await asyncio.sleep(0.05)
        waiting = asyncio.ensure_future(methods.handle("terminal/wait_for_exit", terminal, None))
        await asyncio.sleep(0.1)
        started = asyncio.get_running_loop().time()
        await methods.handle("terminal/release", terminal, None)
        # Released while the escaped child still holds the pipe: the release does
        # not wait the child out, and the agent's wait is answered.
        assert asyncio.get_running_loop().time() - started < 3
        assert await asyncio.wait_for(waiting, 10) == {"exitCode": None, "signal": "SIGKILL"}
    finally:
        await methods.aclose()


async def test_acphostfs_reap_lets_the_reader_reach_eof(tmp_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", stdout=asyncio.subprocess.PIPE
    )
    await process.wait()
    terminal = _Terminal(process, OUTPUT_LIMIT)
    # The output still in the pipe when the command exits takes a moment to read.
    terminal.reader = asyncio.create_task(asyncio.sleep(0.2))
    await ClientMethods._reap(terminal)
    assert terminal.reader.done()
    assert not terminal.reader.cancelled()
    # A reader that never reaches EOF is cancelled after a short grace.
    stuck = _Terminal(process, OUTPUT_LIMIT)
    stuck.reader = asyncio.create_task(asyncio.sleep(60))
    await asyncio.wait_for(ClientMethods._reap(stuck), 5)
    assert stuck.reader.cancelled()


async def test_acphostfs_terminal_env_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("M14_HOST_ONLY", "1")
    monkeypatch.setenv("M14_KEEP", "1")
    monkeypatch.setenv("M14_KEEP_TOKEN", "s")
    allow = ("PATH", "SYSTEMROOT", "M14_KEEP*")
    methods = _methods(ws, policy=None, env_allow=allow)
    script = "import json, os; print(json.dumps(sorted(os.environ)))"
    try:
        created = await methods.handle(
            "terminal/create",
            {
                "command": sys.executable,
                "args": ["-c", script],
                "env": [{"name": "M14_FROM_AGENT", "value": "x"}],
            },
            None,
        )
        terminal = {"terminalId": created["terminalId"]}
        await methods.handle("terminal/wait_for_exit", terminal, None)
        output = await methods.handle("terminal/output", terminal, None)
    finally:
        await methods.aclose()
    names = {name.upper() for name in json.loads(output["output"])}
    assert "M14_KEEP" in names
    assert "M14_FROM_AGENT" in names
    assert "M14_HOST_ONLY" not in names
    assert "M14_KEEP_TOKEN" not in names  # a glob never keeps a secret-shaped name
    assert "PATH" in names


async def test_acphostfs_terminal_ask(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "ask").mkdir(parents=True)
    (ws / "ask" / "a").write_text("x")
    methods = _methods(ws)
    params = {"command": sys.executable, "args": ["-c", "pass", "ask/a"]}
    try:
        approved = RecordingSink()
        created = await methods.handle("terminal/create", params, approved)
        assert created["terminalId"]
        assert len(approved.confirmations) == 1
        assert "'**/ask/*'" in (approved.confirmations[0].invocation_message or "")
        # The policy asked, so an automatic answerer (a flow's `[approvals]` allow
        # rule) must not approve it, as on the agent's own permission requests.
        assert approved.confirmations[0].policy_asked is True
        with pytest.raises(AcpError, match=r"^declined:") as declined:
            await methods.handle("terminal/create", params, RecordingSink(approve=False))
        assert declined.value.code == -32009
        with pytest.raises(AcpError, match="no one to ask"):
            await methods.handle("terminal/create", params, None)
        # A command word naming a denied path is refused outright, no prompt.
        refused = RecordingSink()
        with pytest.raises(AcpError, match=re.escape("'**/.env'")):
            await methods.handle("terminal/create", {"command": "cat", "args": [".env"]}, refused)
        assert refused.confirmations == []
    finally:
        await methods.aclose()


async def test_acphostfs_symlink_escape(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("no")
    try:
        os.symlink(outside, ws / "link", target_is_directory=True)
        os.symlink(outside / "secret.txt", ws / "innocent.txt")
    except OSError:
        pytest.skip("symlinks unavailable")
    # The jail holds even with a policy that allows everything outside the roots.
    methods = _methods(ws, policy=ToolPolicy(allow_paths_outside_roots=True))
    try:
        for params in (
            {"path": str(ws / "link" / "secret.txt")},
            {"path": str(ws / "innocent.txt")},
        ):
            with pytest.raises(AcpError, match="outside") as read:
                await methods.handle("fs/read_text_file", params, None)
            assert read.value.code == -32009
        with pytest.raises(AcpError, match="outside"):
            await methods.handle(
                "fs/write_text_file", {"path": str(ws / "link" / "new.txt"), "content": "x"}, None
            )
        with pytest.raises(AcpError, match="outside"):
            await methods.handle(
                "terminal/create", {"command": sys.executable, "cwd": str(ws / "link")}, None
            )
    finally:
        await methods.aclose()
    assert not (outside / "new.txt").exists()
