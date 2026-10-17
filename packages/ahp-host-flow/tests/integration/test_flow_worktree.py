"""`agent()` with `isolation: 'worktree'` (plan §4.4) over an in-memory host and a temp git repo."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.flow_fakes import GatedProvider, reset_fakes
from tests.flow_hosting import FlowFixture, boot_flow, final_json
from tests.hosting import cancel_turn, notifications, run_turn, start_turn, wait_turn

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH"),
]

TOML = """
[providers.rec]
load = "tests.flow_fakes:RecordingProvider"

[providers.fw]
load = "tests.flow_fakes:FileWriterProvider"

[providers.broken]
load = "tests.flow_fakes:BrokenSessionProvider"

[providers.gate]
load = "tests.flow_fakes:GatedProvider"

[routing]
default = "rec"
rules = [
  { provider = "fw", label = "fw*" },
  { provider = "broken", label = "bk*" },
  { provider = "gate", label = "gate*" },
]
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    reset_fakes()
    yield
    reset_fakes()


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=flow-test", "-c", "user.email=flow-test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def init_repo(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    git(ws, "init", "-q")
    (ws / "README.md").write_text("hello", encoding="utf-8")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "init")


async def boot(tmp_path: Path, node_path: str, *, repo: bool) -> FlowFixture:
    if repo:
        init_repo(tmp_path / "ws")
    return await boot_flow(tmp_path, node_path, TOML)


def run_id_of(turn: object) -> str:
    (line,) = [n for n in notifications(turn) if n.startswith("Run ")]  # type: ignore[arg-type]
    return line.removeprefix("Run ")


def script(message: str) -> str:
    return f"```js\nreturn await agent({message!r}, {{label: 'fw-1', isolation: 'worktree'}})\n```"


async def test_worktree_unchanged_is_removed(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path, repo=True)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", script("noop"))
        path = tmp_path / "state" / "runs" / run_id_of(turn) / "worktrees" / "0"
        assert Path(str(final_json(turn))).resolve() == path.resolve()
        assert not path.exists()
        listed = git(fx.workspace, "worktree", "list", "--porcelain")
        assert listed.count("worktree ") == 1
    finally:
        await fx.shut()


async def test_worktree_changed_is_kept_and_logged(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path, repo=True)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", script("write out.txt"))
        path = tmp_path / "state" / "runs" / run_id_of(turn) / "worktrees" / "0"
        assert (path / "out.txt").exists()
        assert not (fx.workspace / "out.txt").exists()
        prefix = "agent() #0: worktree kept at "
        (kept,) = [n for n in notifications(turn) if n.startswith(prefix)]
        text = kept.removeprefix(prefix).removesuffix(" (it has changes)")
        assert Path(text).resolve() == path.resolve()
    finally:
        await fx.shut()


async def test_worktree_not_a_git_repo(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path, repo=False)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", script("noop"))
        assert final_json(turn) is None
        prefix = "agent() #0 failed: isolation 'worktree' needs a git repository"
        assert [n for n in notifications(turn) if n.startswith(prefix)]
    finally:
        await fx.shut()


def labelled(label: str) -> str:
    return script("x").replace("fw-1", label)


def only_main_worktree(ws: Path) -> bool:
    return git(ws, "worktree", "list", "--porcelain").count("worktree ") == 1


async def test_worktree_released_when_create_session_fails(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path, repo=True)
    try:
        script = labelled("bk-1")
        turn = await run_turn(fx.wire, fx.chat, "t1", script)
        assert final_json(turn) is None
        assert [n for n in notifications(turn) if "no session for you" in n]
        (runs,) = (tmp_path / "state" / "runs").iterdir()
        assert not (runs / "worktrees" / "0").exists()
        assert only_main_worktree(fx.workspace)
    finally:
        await fx.shut()


async def test_worktree_released_when_cancelled_before_the_session(
    tmp_path: Path, node_path: str
) -> None:
    fx = await boot(tmp_path, node_path, repo=True)
    try:
        script = labelled("gate-1")
        await start_turn(fx.wire, fx.chat, "t1", script)
        assert await fx.wire.until(
            lambda: bool(GatedProvider.instances) and GatedProvider.instances[0].creating.is_set()
        )
        (runs,) = (tmp_path / "state" / "runs").iterdir()
        assert (runs / "worktrees" / "0").exists()
        await cancel_turn(fx.wire, fx.chat, "t1")
        assert (await wait_turn(fx.wire, fx.chat, "t1"))["state"] == "cancelled"
        assert await fx.wire.until(lambda: not (runs / "worktrees" / "0").exists())
        assert await fx.wire.until(lambda: only_main_worktree(fx.workspace))
    finally:
        await fx.shut()
