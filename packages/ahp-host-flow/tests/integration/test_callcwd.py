"""`opts.x.cwd` runs an in-process call in a folder inside the run's working directories."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow import catalog
from ahp_host_flow.engine import AgentCall
from ahp_host_flow.engine.journal import call_key
from ahp_host_flow.worktree import release_worktree
from tests import flow_fakes
from tests.flow_fakes import RecordingProvider, RecordingSession
from tests.flow_hosting import boot_flow, final_json
from tests.hosting import notifications, run_turn

TOML = (
    '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n\n[routing]\ndefault = "rec"\n'
)
OUTSIDE = "outside the run's working directories"


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    flow_fakes.reset_fakes()
    yield
    flow_fakes.reset_fakes()


def write_workflow(tmp_path: Path, name: str, body: str) -> None:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    source = f"meta = {{'name': '{name}', 'description': 'd'}}\n\nasync def main(args):\n"
    source += "".join(f"    {line}\n" for line in body.splitlines())
    (user / f"{name}.py").write_text(source, encoding="utf-8")


def sessions() -> list[RecordingSession]:
    return [s for p in RecordingProvider.instances for s in p.sessions]


def session_for(prompt: str) -> RecordingSession:
    (found,) = (s for s in sessions() if any(m.text == prompt for m in s.messages))
    return found


async def test_callcwd_inner_session_gets_folder(tmp_path: Path) -> None:
    (tmp_path / "ws" / "sub").mkdir(parents=True)
    write_workflow(
        tmp_path,
        "cw",
        "return [\n"
        "    await agent('rel', x={'cwd': 'sub'}),\n"
        f"    await agent('abs', x={{'cwd': {str(tmp_path / 'ws' / 'sub')!r}}}),\n"
        f"    await agent('uri', x={{'cwd': {(tmp_path / 'ws' / 'sub').as_uri()!r}}}),\n"
        "    await agent('plain'),\n"
        "]",
    )
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/cw")
        assert final_json(turn) == ["rec: rel", "rec: abs", "rec: uri", "rec: plain"]
        folder = ((fx.workspace / "sub").resolve().as_uri(),)
        for prompt in ("rel", "abs", "uri"):
            assert session_for(prompt).context.working_directories == folder
        assert session_for("plain").context.working_directories == (fx.workspace.as_uri(),)
    finally:
        await fx.shut()


async def test_callcwd_bad_folder_fails_the_call(tmp_path: Path) -> None:
    (tmp_path / "outside").mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hello", encoding="utf-8")
    write_workflow(
        tmp_path,
        "bad",
        "return [\n"
        "    await agent('up', x={'cwd': '../outside'}),\n"
        f"    await agent('abs', x={{'cwd': {str(tmp_path / 'outside')!r}}}),\n"
        "    await agent('file', x={'cwd': 'notes.txt'}),\n"
        "    await agent('gone', x={'cwd': 'missing'}),\n"
        "    await agent('int', x={'cwd': 7}),\n"
        "]",
    )
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/bad")
        assert final_json(turn) == [None] * 5
        notes = notifications(turn)
        assert sum(OUTSIDE in n for n in notes) == 2
        assert any("is not a directory" in n for n in notes)
        assert any("does not exist" in n for n in notes)
        assert any("opts.x.cwd must be a non-empty string" in n for n in notes)
        sent = {m.text for s in sessions() for m in s.messages}
        assert not sent & {"up", "abs", "file", "gone", "int"}
    finally:
        await fx.shut()


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=flow-test", "-c", "user.email=flow-test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_callcwd_worktree_cut_from_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    proj = tmp_path / "ws" / "proj"
    (proj / "sub").mkdir(parents=True)
    git(proj, "init")
    (proj / "README.md").write_text("hello", encoding="utf-8")
    (proj / "sub" / "x.txt").write_text("x", encoding="utf-8")
    git(proj, "add", "-A")
    git(proj, "commit", "-m", "init")
    # An unchanged worktree is removed when its call ends, so look inside it just before release.
    seen: list[bool] = []
    real_release = release_worktree

    async def spy(wt: Any) -> bool:
        seen.append((wt.cwd.parent / "README.md").is_file() and (wt.cwd / "x.txt").is_file())
        return await real_release(wt)

    monkeypatch.setattr("ahp_host_flow.dispatch.release_worktree", spy)
    write_workflow(
        tmp_path,
        "wt",
        "a = await agent('wt', x={'cwd': 'proj/sub'}, isolation='worktree')\n"
        "b = await agent('plain-wt', isolation='worktree')\n"
        "return [a, b]",
    )
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/wt")
        assert final_json(turn) == ["rec: wt", None]
        (uri,) = session_for("wt").context.working_directories
        cwd = catalog.directory_of(uri)
        assert cwd is not None
        assert cwd.name == "sub"
        assert cwd.is_relative_to((tmp_path / "state" / "runs").resolve())
        assert seen == [True]
        assert any("needs a git repository" in n for n in notifications(turn))
    finally:
        await fx.shut()


async def test_callcwd_named_chat_keeps_first_folder(tmp_path: Path) -> None:
    (tmp_path / "ws" / "a").mkdir(parents=True)
    (tmp_path / "ws" / "b").mkdir()
    write_workflow(
        tmp_path,
        "nc",
        "one = await agent('one', x={'chat': 'c', 'cwd': 'a'})\n"
        "two = await agent('two', x={'chat': 'c', 'cwd': 'b'})\n"
        "return [one, two]",
    )
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/nc")
        assert final_json(turn) == ["rec: one", "rec: two"]
        (session,) = sessions()
        assert [m.text for m in session.messages] == ["one", "two"]
        assert session.context.working_directories == ((fx.workspace / "a").resolve().as_uri(),)
        assert any("keeps its first call's folder" in n for n in notifications(turn))
    finally:
        await fx.shut()


async def test_callcwd_unknown_isolation_logs_a_line(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    write_workflow(tmp_path, "iso", "return await agent('iso', isolation='container')")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/iso")
        assert final_json(turn) == "rec: iso"
        lines = [n for n in notifications(turn) if "isolation 'container' is not supported" in n]
        assert len(lines) == 1
        assert "isolation 'container' is not supported; ignored" in lines[0]
    finally:
        await fx.shut()


def test_callcwd_key_covers_cwd() -> None:
    a = call_key(AgentCall(0, "p", {"x": {"cwd": "a"}}))
    b = call_key(AgentCall(0, "p", {"x": {"cwd": "b"}}))
    assert a != b
