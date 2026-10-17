"""Git worktrees for ``opts.isolation == 'worktree'`` (plan §4.4).

A call gets a detached worktree of the run's repository under the run's state
directory. It is removed afterwards when nothing changed in it and kept
otherwise; changesets per child are M8.
"""

from __future__ import annotations

import asyncio
from asyncio.subprocess import DEVNULL, PIPE
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ahp_host_flow.engine import run_dir

WORKTREES_DIR: Final = "worktrees"


class WorktreeError(RuntimeError):
    """A worktree could not be created or released; the message is user-facing."""


@dataclass(frozen=True)
class Worktree:
    repo: Path
    path: Path
    cwd: Path
    base: str


def worktree_path(state_dir: Path, run_id: str, ordinal: int) -> Path:
    return run_dir(state_dir, run_id) / WORKTREES_DIR / str(ordinal)


async def _git(*args: str, cwd: Path) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd, stdin=DEVNULL, stdout=PIPE, stderr=PIPE
        )
    except FileNotFoundError:
        raise WorktreeError(
            "isolation 'worktree' needs git, and git was not found on PATH"
        ) from None
    out, err = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


async def create_worktree(workdir: Path, dest: Path) -> Worktree:
    not_a_repo = WorktreeError(
        f"isolation 'worktree' needs a git repository, and {workdir} is not in one"
    )
    if not workdir.is_dir():
        raise not_a_repo
    code, out, _ = await _git("rev-parse", "--show-toplevel", cwd=workdir)
    if code != 0:
        raise not_a_repo
    repo = Path(out.strip()).resolve()
    code, out, _ = await _git("rev-parse", "--verify", "HEAD", cwd=repo)
    if code != 0:
        raise WorktreeError(
            f"isolation 'worktree' needs a commit to check out, and {repo} has none"
        )
    base = out.strip()
    if dest.exists():
        raise WorktreeError(f"{dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    code, _, err = await _git("worktree", "add", "--detach", str(dest), base, cwd=repo)
    if code != 0:
        raise WorktreeError(f"git worktree add failed: {err.strip()}")
    try:
        cwd = dest / workdir.resolve().relative_to(repo)
    except ValueError:
        cwd = dest
    return Worktree(repo=repo, path=dest, cwd=cwd, base=base)


async def has_changes(wt: Worktree) -> bool:
    code, out, _ = await _git("status", "--porcelain", "--untracked-files=all", cwd=wt.path)
    if code != 0 or out.strip():
        return True
    code, out, _ = await _git("rev-parse", "HEAD", cwd=wt.path)
    return code != 0 or out.strip() != wt.base


async def release_worktree(wt: Worktree) -> bool:
    """Remove an unchanged worktree; True when it was kept because it has changes."""
    if await has_changes(wt):
        return True
    code, _, err = await _git("worktree", "remove", str(wt.path), cwd=wt.repo)
    if code != 0:
        raise WorktreeError(f"git worktree remove failed: {err.strip()}")
    return False
