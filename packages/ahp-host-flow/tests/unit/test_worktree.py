from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ahp_host_flow.worktree import (
    WorktreeError,
    create_worktree,
    release_worktree,
    worktree_path,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")

IDENT = ["-c", "user.name=flow-test", "-c", "user.email=flow-test@example.com"]


@pytest.fixture(autouse=True)
def _ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def _git(*args: str, cwd: Path) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout


def _repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "sub").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("hello")
    (repo / "sub" / "b.txt").write_text("sub")
    _git("add", "-A", cwd=repo)
    _git(*IDENT, "commit", "-q", "-m", "init", cwd=repo)
    return repo


def _trees(repo: Path) -> list[str]:
    out = _git("worktree", "list", "--porcelain", cwd=repo)
    return [line for line in out.splitlines() if line.startswith("worktree ")]


def test_worktree_path_layout(tmp_path: Path) -> None:
    assert worktree_path(tmp_path, "r1", 3) == tmp_path / "runs" / "r1" / "worktrees" / "3"


async def test_worktree_unchanged_is_removed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    dest = worktree_path(tmp_path / "state", "r1", 1)
    wt = await create_worktree(repo, dest)
    assert wt.path == dest
    assert (dest / "a.txt").read_text() == "hello"
    assert await release_worktree(wt) is False
    assert not dest.exists()
    assert len(_trees(repo)) == 1


async def test_worktree_untracked_file_is_kept(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    wt = await create_worktree(repo, worktree_path(tmp_path / "state", "r1", 1))
    (wt.cwd / "new.txt").write_text("x")
    assert await release_worktree(wt) is True
    assert wt.path.exists()


async def test_worktree_commit_is_kept(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    wt = await create_worktree(repo, worktree_path(tmp_path / "state", "r1", 1))
    (wt.path / "a.txt").write_text("changed")
    _git(*IDENT, "commit", "-q", "-am", "edit", cwd=wt.path)
    assert await release_worktree(wt) is True
    assert wt.path.exists()


async def test_worktree_subdirectory_cwd(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    wt = await create_worktree(repo / "sub", worktree_path(tmp_path / "state", "r1", 1))
    assert wt.cwd == wt.path / "sub"
    assert (wt.cwd / "b.txt").exists()


async def test_worktree_plain_directory_fails(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="needs a git repository"):
        await create_worktree(plain, tmp_path / "wt")


async def test_worktree_without_commits_fails(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    with pytest.raises(WorktreeError, match="needs a commit"):
        await create_worktree(repo, tmp_path / "wt")


async def test_worktree_existing_destination_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    dest = tmp_path / "wt"
    dest.mkdir()
    with pytest.raises(WorktreeError, match="already exists"):
        await create_worktree(repo, dest)
