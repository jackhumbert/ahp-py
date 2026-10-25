"""Worktree helpers: a kept worktree's changes, its patch, apply and discard (plan §4.4)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ahp_host_flow.worktree import (
    WorktreeError,
    apply_to_repo,
    commit_onto_repo,
    create_worktree,
    discard_worktree,
    release_worktree,
    worktree_changes,
    worktree_patch,
    worktree_path,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH")


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


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
    (ws / "old.txt").write_text("old", encoding="utf-8")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "init")


async def test_worktree_changes_lists_added_modified_deleted(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    init_repo(ws)
    wt = await create_worktree(ws, tmp_path / "wt")
    (wt.path / "new.txt").write_text("x", encoding="utf-8")
    (wt.path / "README.md").write_text("hello\nworld", encoding="utf-8")
    (wt.path / "old.txt").unlink()

    changes = await worktree_changes(wt)

    assert [c.uri for c in changes] == [
        (wt.repo / name).as_uri() for name in ("README.md", "new.txt", "old.txt")
    ]
    readme, new, old = changes
    assert (readme.before, readme.after) == (b"hello", b"hello\nworld")
    assert (new.before, new.after) == (None, b"x")
    assert (old.before, old.after) == (b"old", None)


async def test_worktree_patch_applies_to_repo(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    init_repo(ws)
    wt = await create_worktree(ws, tmp_path / "wt")
    (wt.path / "new.txt").write_text("x", encoding="utf-8")
    (wt.path / "README.md").write_text("hello\nworld", encoding="utf-8")
    (wt.path / "old.txt").unlink()

    await apply_to_repo(wt, await worktree_patch(wt))

    assert (ws / "new.txt").read_text(encoding="utf-8") == "x"
    assert (ws / "README.md").read_text(encoding="utf-8") == "hello\nworld"
    assert not (ws / "old.txt").exists()
    await apply_to_repo(wt, b"")

    await discard_worktree(wt)
    assert not wt.path.exists()
    assert str(wt.path.name) not in git(ws, "worktree", "list")


async def test_worktree_resolve_failures(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    init_repo(ws)
    wt = await create_worktree(ws, tmp_path / "wt")
    (wt.path / "README.md").write_text("from worktree", encoding="utf-8")
    patch = await worktree_patch(wt)
    (ws / "README.md").write_text("edited in repo", encoding="utf-8")

    with pytest.raises(WorktreeError, match=r"^git apply failed"):
        await apply_to_repo(wt, patch)


def _repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "sub").mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("hello")
    (repo / "sub" / "b.txt").write_text("sub")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def _trees(repo: Path) -> list[str]:
    out = git(repo, "worktree", "list", "--porcelain")
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
    git(wt.path, "commit", "-q", "-am", "edit")
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
    git(repo, "init", "-q")
    with pytest.raises(WorktreeError, match="needs a commit"):
        await create_worktree(repo, tmp_path / "wt")


async def test_worktree_existing_destination_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    dest = tmp_path / "wt"
    dest.mkdir()
    with pytest.raises(WorktreeError, match="already exists"):
        await create_worktree(repo, dest)


BINARY = b"\x00\r\n\x01\r\n\xff\r"
EDITED_BINARY = b"\x00\r\n\x02\r\n\xff\r"


def git_bytes(cwd: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def crlf_repo(ws: Path, *, autocrlf: bool) -> None:
    """A repo with a CRLF text file and a binary file (NUL and CRLF bytes)."""
    ws.mkdir(parents=True, exist_ok=True)
    git(ws, "init", "-q")
    git(ws, "config", "core.autocrlf", "true" if autocrlf else "false")
    git(ws, "config", "core.safecrlf", "false")
    (ws / "text.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    (ws / "bin.dat").write_bytes(BINARY)
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "init")


@pytest.mark.parametrize("autocrlf", [False, True])
async def test_worktree_changes_compare_like_with_like(tmp_path: Path, autocrlf: bool) -> None:
    ws = tmp_path / "ws"
    crlf_repo(ws, autocrlf=autocrlf)
    wt = await create_worktree(ws, tmp_path / "wt")
    (wt.path / "text.txt").write_bytes(b"one\r\nTWO\r\nthree\r\n")
    (wt.path / "bin.dat").write_bytes(EDITED_BINARY)

    changes = {c.uri.rsplit("/", 1)[-1]: c for c in await worktree_changes(wt)}

    # A binary file is byte-exact on both sides, whatever core.autocrlf says.
    assert changes["bin.dat"].before == BINARY
    assert changes["bin.dat"].after == EDITED_BINARY
    # Text is compared as git stores it (LF with autocrlf, verbatim without), on both sides,
    # so only the edited line differs.
    eol = b"\n" if autocrlf else b"\r\n"
    text = changes["text.txt"]
    assert text.before == b"one" + eol + b"two" + eol + b"three" + eol
    assert text.after == b"one" + eol + b"TWO" + eol + b"three" + eol
    assert text.before == git_bytes(ws, "show", "HEAD:text.txt")


@pytest.mark.parametrize("autocrlf", [False, True])
async def test_worktree_revert_restores_exact_original_bytes(
    tmp_path: Path, autocrlf: bool
) -> None:
    ws = tmp_path / "ws"
    crlf_repo(ws, autocrlf=autocrlf)
    wt = await create_worktree(ws, tmp_path / "wt")
    (wt.path / "text.txt").write_bytes(b"one\r\nTWO\r\nthree\r\n")
    (wt.path / "bin.dat").write_bytes(EDITED_BINARY)
    changes = await worktree_changes(wt)
    await apply_to_repo(wt, await worktree_patch(wt))
    assert (ws / "bin.dat").read_bytes() == EDITED_BINARY

    for change in changes:  # a revert writes every `before` back
        assert change.before is not None
        (ws / change.uri.rsplit("/", 1)[-1]).write_bytes(change.before)

    assert (ws / "bin.dat").read_bytes() == BINARY
    assert (ws / "text.txt").read_bytes() == git_bytes(ws, "show", "HEAD:text.txt")


def identity(repo: Path) -> None:
    git(repo, "config", "user.name", "flow-test")
    git(repo, "config", "user.email", "flow-test@example.com")


def committed(repo: Path) -> list[str]:
    return git(repo, "show", "--name-only", "--format=", "HEAD").split()


async def test_worktree_commit_paths_are_literal(tmp_path: Path) -> None:
    """An agent's file named `*.txt` is not a pathspec: the user's notes.txt stays out."""
    ws = tmp_path / "ws"
    init_repo(ws)
    identity(ws)
    (ws / "notes.txt").write_text("v1", encoding="utf-8")
    git(ws, "add", "notes.txt")
    git(ws, "commit", "-q", "-m", "notes")
    wt = await create_worktree(ws, tmp_path / "wt")
    (wt.path / "*.txt").write_text("star", encoding="utf-8")
    (ws / "notes.txt").write_text("the user's own edit", encoding="utf-8")

    assert [c.uri.rsplit("/", 1)[-1] for c in await worktree_changes(wt)] == ["%2A.txt"]
    await commit_onto_repo(wt, message="agent")

    assert committed(ws) == ["*.txt"]
    assert "notes.txt" in git(ws, "status", "--porcelain")


async def test_worktree_leading_dash_names(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    init_repo(ws)
    identity(ws)
    wt = await create_worktree(ws, tmp_path / "wt")
    (wt.path / "-n").write_text("dash", encoding="utf-8")
    (wt.path / "--all").write_text("dashes", encoding="utf-8")

    changes = await worktree_changes(wt)
    assert sorted(c.after or b"" for c in changes) == [b"dash", b"dashes"]
    await commit_onto_repo(wt, message="agent")
    assert sorted(committed(ws)) == ["--all", "-n"]


NON_UTF8 = os.fsdecode(b"caf\xe9.txt")


@pytest.mark.skipif(sys.platform != "linux", reason="needs a filesystem taking any bytes")
async def test_worktree_non_utf8_names(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    init_repo(ws)
    identity(ws)
    wt = await create_worktree(ws, tmp_path / "wt")
    (wt.path / NON_UTF8).write_bytes(b"latin")

    (change,) = await worktree_changes(wt)
    assert change.uri == (wt.repo / NON_UTF8).as_uri()
    assert (change.before, change.after) == (None, b"latin")
    await apply_to_repo(wt, await worktree_patch(wt))
    assert (ws / NON_UTF8).read_bytes() == b"latin"
    (ws / NON_UTF8).unlink()

    await commit_onto_repo(wt, message="agent")
    assert (ws / NON_UTF8).read_bytes() == b"latin"
    assert "caf" in git(ws, "show", "--name-only", "--format=", "HEAD")
