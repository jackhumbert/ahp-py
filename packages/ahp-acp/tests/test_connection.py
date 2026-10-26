from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ahp_acp import (
    AcpConnection,
    AcpError,
    AgentExitedError,
    MethodNotFoundError,
    RequestTimeoutError,
    kill_tree,
    signal_group,
)
from tests.support import argv_for


async def _noop_notification(method: str, params: Any) -> None:
    return None


async def _no_requests(method: str, params: Any) -> Any:
    raise MethodNotFoundError(method)


async def spawn(script: dict[str, Any], **kwargs: Any) -> AcpConnection:
    kwargs.setdefault("grace", 0.3)
    kwargs.setdefault("on_notification", _noop_notification)
    kwargs.setdefault("on_request", _no_requests)
    return await AcpConnection.spawn(argv_for(script), **kwargs)


async def eventually(check: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.02)


async def test_request_response_and_error() -> None:
    conn = await spawn(
        {
            "responses": {"ping": {"ok": True}},
            "errors": {"boom": {"code": -32000, "message": "bad", "data": {"why": 1}}},
        }
    )
    try:
        assert await conn.request("ping", {}) == {"ok": True}
        with pytest.raises(AcpError) as info:
            await conn.request("boom")
        assert (info.value.code, info.value.message, info.value.data) == (-32000, "bad", {"why": 1})
        assert conn.pid is not None
        assert conn.returncode is None
    finally:
        await conn.aclose()
    assert conn.returncode is not None


async def test_request_timeout() -> None:
    conn = await spawn({"before": {"slow": [{"sleep": 5}]}})
    try:
        with pytest.raises(RequestTimeoutError):
            await conn.request("slow", timeout=0.2)
    finally:
        await conn.aclose()


async def test_agent_exit_fails_pending_requests() -> None:
    conn = await spawn({"before": {"die": [{"stderr": "last words"}, {"exit": 3}]}})
    try:
        with pytest.raises(AgentExitedError, match="last words"):
            await conn.request("die")
        assert "last words" in conn.stderr_tail
    finally:
        await conn.aclose()


async def test_notifications_arrive_in_order_before_the_response() -> None:
    seen: list[str] = []

    async def on_notification(method: str, params: Any) -> None:
        await asyncio.sleep(0.01)
        seen.append(params["n"])

    conn = await spawn(
        {
            "before": {
                "go": [
                    {"send": {"method": "_example/n", "params": {"n": str(i)}}} for i in range(5)
                ]
            },
            "responses": {"go": {}},
        },
        on_notification=on_notification,
    )
    try:
        await conn.request("go")
        assert seen == ["0", "1", "2", "3", "4"]
    finally:
        await conn.aclose()


async def test_handler_errors_become_json_rpc_errors() -> None:
    async def on_request(method: str, params: Any) -> Any:
        if method == "_example/missing":
            raise MethodNotFoundError(method)
        raise RuntimeError("kaput")

    steps: list[dict[str, Any]] = [
        {"send": {"id": "a", "method": "_example/missing", "params": {}}},
        {"send": {"id": "b", "method": "_example/other", "params": {}}},
        {"await": ["a", "b"]},
    ]
    conn = await spawn(
        {"before": {"go": steps}, "responses": {"go": {"$replies": True}}}, on_request=on_request
    )
    try:
        replies = (await conn.request("go"))["replies"]
        assert replies["a"]["code"] == -32601
        assert replies["b"]["code"] == -32603
        assert "kaput" in replies["b"]["message"]
    finally:
        await conn.aclose()


async def test_oversize_line_is_skipped() -> None:
    update = {"method": "_example/after", "params": {}}
    got: list[str] = []

    async def on_notification(method: str, params: Any) -> None:
        got.append(method)

    conn = await spawn(
        {
            "before": {"go": [{"big_line": 200_000}, {"send": update}]},
            "responses": {"go": {"done": True}},
        },
        stream_limit=1000,
        on_oversize="skip",
        on_notification=on_notification,
    )
    try:
        assert await conn.request("go") == {"done": True}
        assert got == ["_example/after"]
    finally:
        await conn.aclose()


async def test_oversize_line_closes() -> None:
    conn = await spawn(
        {"before": {"go": [{"big_line": 200_000}]}}, stream_limit=1000, on_oversize="close"
    )
    try:
        with pytest.raises(AgentExitedError):
            await conn.request("go")
    finally:
        await conn.aclose()


async def test_stray_stdout_lines_are_ignored() -> None:
    conn = await spawn(
        {"before": {"go": [{"raw": "a banner"}, {"raw": ""}]}, "responses": {"go": {"k": 1}}}
    )
    try:
        assert await conn.request("go") == {"k": 1}
    finally:
        await conn.aclose()


async def test_stderr_callback() -> None:
    lines: list[str] = []
    conn = await spawn(
        {"before": {"go": [{"stderr": "one"}, {"stderr": "two"}]}},
        on_stderr=lines.append,
    )
    try:
        await conn.request("go")
        await eventually(lambda: lines == ["one", "two"])
    finally:
        await conn.aclose()


async def test_stderr_tail_without_a_callback() -> None:
    conn = await spawn({"before": {"go": [{"stderr": "hello"}]}})
    try:
        await conn.request("go")
        await eventually(lambda: conn.stderr_tail == "hello")
    finally:
        await conn.aclose()


@pytest.mark.parametrize("stream_limit", [None, 64 * 1024])
async def test_a_long_stderr_line_does_not_stop_the_drain(stream_limit: int | None) -> None:
    # A line past the reader's limit used to end the drain for good; the pipe
    # then filled and the agent blocked writing to it, so no reply ever came.
    # With the default limit the line is read; with a small one it is dropped.
    steps = [{"big_stderr": 100_000}, {"stderr_fill": 256}, {"stderr": "after"}]
    kwargs: dict[str, Any] = {} if stream_limit is None else {"stream_limit": stream_limit}
    conn = await spawn({"responses": {"go": {"ok": True}}, "before": {"go": steps}}, **kwargs)
    try:
        assert await conn.request("go", timeout=10) == {"ok": True}
        await eventually(lambda: conn.stderr_tail.endswith("after"))
    finally:
        await conn.aclose()


async def _env_of(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> dict[str, str]:
    monkeypatch.setenv("AHP_ACP_TEST_PARENT_VAR", "parent")
    got: dict[str, Any] = {}

    async def on_notification(method: str, params: Any) -> None:
        if method == "_example/env":
            got.update(params["env"])

    conn = await spawn(
        {"before": {"go": [{"env": True}]}}, on_notification=on_notification, **kwargs
    )
    try:
        await conn.request("go")
    finally:
        await conn.aclose()
    return {str(k): str(v) for k, v in got.items()}


async def test_env_is_merged_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    env = await _env_of(monkeypatch, env={"EXTRA": "1"})
    assert env["AHP_ACP_TEST_PARENT_VAR"] == "parent"
    assert env["EXTRA"] == "1"


async def test_env_is_exact_without_inheritance(monkeypatch: pytest.MonkeyPatch) -> None:
    env = await _env_of(monkeypatch, env={"EXTRA": "1"}, inherit_env=False)
    assert env["EXTRA"] == "1"
    assert "AHP_ACP_TEST_PARENT_VAR" not in env
    assert "HOME" not in env
    # The interpreter or the OS may add a locale or a few platform variables
    # (macOS gives every process __CF_USER_TEXT_ENCODING).
    platform = {
        "LC_CTYPE",
        "SYSTEMROOT",
        "PYTHONUTF8",
        "PYTHONCOERCECLOCALE",
        "__CF_USER_TEXT_ENCODING",
    }
    assert set(env) - {"EXTRA"} <= platform


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_process_group_kill_reaps_a_grandchild() -> None:
    pids: list[int] = []

    async def on_notification(method: str, params: Any) -> None:
        if method == "_example/pid":
            pids.append(params["pid"])

    # The agent ignores stdin EOF and SIGTERM, and so does its child: only the
    # final SIGKILL to the group ends them.
    conn = await spawn(
        {"ignore_eof": True, "before": {"go": [{"grandchild": True}]}},
        on_notification=on_notification,
        grace=0.2,
    )
    await conn.request("go")
    assert len(pids) == 1
    assert not _gone(pids[0])
    await conn.aclose()
    await eventually(lambda: _gone(pids[0]))
    assert conn.returncode is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_without_a_process_group_only_the_agent_is_signalled() -> None:
    conn = await spawn({"ignore_eof": True}, new_process_group=False, grace=0.2)
    await conn.request("noop")
    await conn.aclose()
    assert conn.returncode is not None


async def test_bad_oversize_policy() -> None:
    with pytest.raises(ValueError, match="on_oversize"):
        AcpConnection(
            asyncio.StreamReader(),
            object(),
            on_notification=_noop_notification,
            on_request=_no_requests,
            on_oversize="bogus",  # type: ignore[arg-type]
        )


class _SpawnedError(Exception):
    def __init__(self, kwargs: dict[str, Any]) -> None:
        super().__init__("spawned")
        self.kwargs = kwargs


async def _spawn_kwargs(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> dict[str, Any]:
    async def fake_exec(*argv: Any, **spawn_kwargs: Any) -> Any:
        raise _SpawnedError(spawn_kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(_SpawnedError) as caught:
        await spawn({}, **kwargs)
    return caught.value.kwargs


@pytest.mark.skipif(sys.platform == "win32", reason="user and groups are POSIX-only")
async def test_user_drops_supplementary_groups_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = await _spawn_kwargs(monkeypatch, user="nobody")
    assert kwargs["user"] == "nobody"
    assert kwargs["extra_groups"] == []


def _fake_passwd(monkeypatch: pytest.MonkeyPatch) -> None:
    import pwd

    entry = pwd.struct_passwd(("me", "x", 4141, 4242, "", "/Users/me", "/bin/sh"))

    def by_name(name: str) -> pwd.struct_passwd:
        if name != "me":
            raise KeyError(name)
        return entry

    def by_uid(uid: int) -> pwd.struct_passwd:
        if uid != 4141:
            raise KeyError(uid)
        return entry

    monkeypatch.setattr(pwd, "getpwnam", by_name)
    monkeypatch.setattr(pwd, "getpwuid", by_uid)


@pytest.mark.skipif(sys.platform == "win32", reason="user and groups are POSIX-only")
@pytest.mark.parametrize("user", ["me", 4141])
@pytest.mark.parametrize("inherit_env", [True, False])
async def test_user_gets_its_primary_group_and_identity(
    monkeypatch: pytest.MonkeyPatch, user: str | int, inherit_env: bool
) -> None:
    _fake_passwd(monkeypatch)
    monkeypatch.setenv("HOME", "/home/caller")
    kwargs = await _spawn_kwargs(
        monkeypatch, user=user, inherit_env=inherit_env, env={"EXTRA": "1"}
    )
    assert kwargs["user"] == user
    assert kwargs["group"] == 4242
    env = kwargs["env"]
    assert (env["HOME"], env["USER"], env["LOGNAME"], env["EXTRA"]) == (
        "/Users/me",
        "me",
        "me",
        "1",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="user and groups are POSIX-only")
async def test_user_does_not_inherit_this_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Another user gets its identity, PATH and the locale, not this process's
    # secrets; inherit_env=True passes everything on, as without `user`.
    _fake_passwd(monkeypatch)
    monkeypatch.setenv("AHP_ACP_TEST_SECRET", "s3cret")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    kwargs = await _spawn_kwargs(monkeypatch, user="me", env={"EXTRA": "1"})
    env = kwargs["env"]
    assert "AHP_ACP_TEST_SECRET" not in env
    assert (env["HOME"], env["USER"], env["EXTRA"]) == ("/Users/me", "me", "1")
    assert env["PATH"] == os.environ["PATH"]
    assert env["LC_ALL"] == "C.UTF-8"
    inherited = await _spawn_kwargs(monkeypatch, user="me", inherit_env=True)
    assert inherited["env"]["AHP_ACP_TEST_SECRET"] == "s3cret"


@pytest.mark.skipif(sys.platform == "win32", reason="user and groups are POSIX-only")
async def test_user_identity_yields_to_explicit_env_and_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_passwd(monkeypatch)
    kwargs = await _spawn_kwargs(monkeypatch, user="me", group=7, env={"HOME": "/srv/agent"})
    assert kwargs["group"] == 7
    assert kwargs["env"]["HOME"] == "/srv/agent"
    assert kwargs["env"]["USER"] == "me"


@pytest.mark.skipif(sys.platform == "win32", reason="user and groups are POSIX-only")
async def test_user_without_a_passwd_entry_needs_a_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_passwd(monkeypatch)
    with pytest.raises(ValueError, match="group"):
        await spawn({}, user=9999)
    kwargs = await _spawn_kwargs(monkeypatch, user=9999, group=9999)
    assert (kwargs["user"], kwargs["group"]) == (9999, 9999)
    # No entry to take an identity from, and this process's own is not handed on.
    assert "LOGNAME" not in kwargs["env"]
    assert kwargs["env"]["PATH"] == os.environ["PATH"]


@pytest.mark.skipif(sys.platform == "win32", reason="user and groups are POSIX-only")
async def test_extra_groups_keeps_the_named_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = await _spawn_kwargs(monkeypatch, user="nobody", extra_groups=(100, "staff"))
    assert kwargs["extra_groups"] == [100, "staff"]


async def _leader_with_child() -> tuple[asyncio.subprocess.Process, int]:
    """A session leader whose grandchild survives it: (the leader, the child's pid)."""
    script = "sleep 60 >/dev/null 2>&1 & echo $!; exit 0"
    process = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    child = int((await process.stdout.readline()).decode())
    await process.wait()
    return process, child


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_kill_tree_kills_a_grandchild_the_leader_left_behind() -> None:
    process, child = await _leader_with_child()
    os.kill(child, 0)  # still running, though its leader exited
    await kill_tree(process)
    await eventually(lambda: not _running(child))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_signal_group_ignores_a_group_that_is_gone() -> None:
    import signal

    process, child = await _leader_with_child()
    signal_group(process, signal.SIGKILL)
    await eventually(lambda: not _running(child))
    signal_group(process, signal.SIGKILL)  # nothing left: not an error
    signal_group(process, signal.SIGKILL, group=False)


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie, not yet reaped by init, is dead
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True
