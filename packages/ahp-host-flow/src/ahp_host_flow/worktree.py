"""Git worktrees for ``opts.isolation == 'worktree'`` (plan §4.4).

A call gets a detached worktree of the run's repository under the run's state
directory. It is removed afterwards when nothing changed in it and kept
otherwise. A kept worktree's changes are read here (`worktree_changes`,
`worktree_patch`) and resolved here (`apply_to_repo`, `discard_worktree`); dispatch
publishes them as a changeset on the worker chat (plan §4.4).
"""

from __future__ import annotations

import asyncio
import dataclasses
from asyncio.subprocess import DEVNULL, PIPE
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from ahp_host.provider.base import SessionPublisher
from ahp_host.provider.changes import Changeset, ChangesetOperation, FileChange

from ahp_host_flow.engine import run_dir

if TYPE_CHECKING:
    from ahp_host.core import Host

WORKTREES_DIR: Final = "worktrees"
APPLY_OPERATION: Final = "ahp-host-flow.worktree.apply"
DISCARD_OPERATION: Final = "ahp-host-flow.worktree.discard"


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


async def _git_bytes(*args: str, cwd: Path, stdin: bytes | None = None) -> tuple[int, bytes, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdin=DEVNULL if stdin is None else PIPE,
            stdout=PIPE,
            stderr=PIPE,
        )
    except FileNotFoundError:
        raise WorktreeError(
            "isolation 'worktree' needs git, and git was not found on PATH"
        ) from None
    out, err = await proc.communicate(stdin)
    return (
        proc.returncode if proc.returncode is not None else -1,
        out,
        err.decode("utf-8", errors="replace"),
    )


async def _git(*args: str, cwd: Path) -> tuple[int, str, str]:
    code, out, err = await _git_bytes(*args, cwd=cwd)
    return code, out.decode("utf-8", errors="replace"), err


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


async def _stage(wt: Worktree) -> None:
    code, _, err = await _git("add", "-A", cwd=wt.path)
    if code != 0:
        raise WorktreeError(f"git add failed: {err}")


async def worktree_changes(wt: Worktree) -> list[FileChange]:
    """The kept worktree's file changes against its base, at the paths they land on apply."""
    await _stage(wt)
    code, out, err = await _git_bytes(
        "diff", "--cached", "--name-status", "-z", "--no-renames", wt.base, cwd=wt.path
    )
    if code != 0:
        raise WorktreeError(f"git diff failed: {err.strip()}")
    # With -z flag, output is null-byte separated: status\0path\0status\0path\0...
    fields = out.rstrip(b"\0").split(b"\0")
    fields_decoded = [f.decode("utf-8", errors="replace") for f in fields]
    pairs = {fields_decoded[i + 1]: fields_decoded[i] for i in range(0, len(fields_decoded) - 1, 2)}
    changes: list[FileChange] = []
    for path in sorted(pairs):
        status = pairs[path]
        before: bytes | None = None
        after: bytes | None = None
        if status != "A":
            code, before, err = await _git_bytes("show", f"{wt.base}:{path}", cwd=wt.path)
            if code != 0:
                raise WorktreeError(f"git show failed: {err.strip()}")
        if status != "D":
            # Read from the index, which `_stage` just filled: `before` is a stored blob, so
            # `after` must be one too. The file on disk is not comparable (core.autocrlf and
            # attributes convert on the way in), and rewriting its bytes would corrupt a
            # binary file. A binary file is byte-exact either way (git does not convert one).
            code, after, err = await _git_bytes("show", f":0:{path}", cwd=wt.path)
            if code != 0:
                raise WorktreeError(f"git show failed: {err.strip()}")
        changes.append(FileChange(uri=(wt.repo / path).as_uri(), before=before, after=after))
    return changes


async def worktree_patch(wt: Worktree) -> bytes:
    """The kept worktree's changes as a binary-safe patch against its base."""
    await _stage(wt)
    code, out, err = await _git_bytes("diff", "--cached", "--binary", wt.base, cwd=wt.path)
    if code != 0:
        raise WorktreeError(f"git diff failed: {err.strip()}")
    return out


async def apply_to_repo(wt: Worktree, patch: bytes) -> None:
    if not patch:
        return
    code, _, err = await _git_bytes(
        "apply", "--binary", "--whitespace=nowarn", "-", cwd=wt.repo, stdin=patch
    )
    if code != 0:
        raise WorktreeError(f"git apply failed: {err.strip()}")


async def discard_worktree(wt: Worktree) -> None:
    code, _, err = await _git("worktree", "remove", "--force", str(wt.path), cwd=wt.repo)
    if code != 0:
        raise WorktreeError(f"git worktree remove failed: {err.strip()}")


@dataclass
class KeptWorktree:
    worktree: Worktree
    publisher: SessionPublisher
    chat: str
    changeset: Changeset


class WorktreeChangesets:
    """Kept worktrees published as changesets on their worker chats, with Apply and Discard.

    The worktree stays until the changeset is resolved: it is the agent's work, so neither
    the end of the run nor the session closing removes it. The held state is in memory, so
    a host restart leaves the worktree on disk and its changeset unresolvable.
    """

    def __init__(self) -> None:
        #: Whether the host can invoke the operations (`register_worktree_operations`); a
        #: host without that hook (the `flow` agent of ahp-node) gets a changeset with none.
        self.operations_registered = False
        self._kept: dict[str, KeptWorktree] = {}

    async def publish(
        self,
        wt: Worktree,
        publisher: SessionPublisher,
        *,
        chat: str,
        title: str,
        ordinal: int,
    ) -> str | None:
        """Publish *wt*'s changes on *chat*; the changeset's URI, or None when there are none."""
        changes = await worktree_changes(wt)
        if not changes:
            return None
        operations = (
            (
                ChangesetOperation(
                    APPLY_OPERATION,
                    "Apply",
                    description=(
                        "Apply this agent's changes to the working tree and remove its worktree"
                    ),
                    icon="check",
                    group="1_flow",
                ),
                ChangesetOperation(
                    DISCARD_OPERATION,
                    "Discard",
                    description="Drop this agent's changes and remove its worktree",
                    icon="discard",
                    group="1_flow",
                    confirmation="Discard this agent's changes and remove its worktree?",
                ),
            )
            if self.operations_registered
            else ()
        )
        changeset = Changeset(
            label=f"{title} (worktree)",
            description=f"agent() #{ordinal}: kept at {wt.path}",
            reviewable=True,
            operations=operations,
        )
        uri = await publisher.changes_published(changeset, changes, chat=chat)
        self._kept[uri] = KeptWorktree(wt, publisher, chat, changeset)
        return uri

    def _lookup(self, uri: str) -> KeptWorktree:
        kept = self._kept.get(uri)
        if kept is None:
            raise RuntimeError(
                "no worktree is kept for this changeset (already resolved, or the host restarted)"
            )
        return kept

    async def apply(self, uri: str) -> None:
        kept = self._lookup(uri)
        await apply_to_repo(kept.worktree, await worktree_patch(kept.worktree))
        await self._resolve(uri, kept, "Applied to the working tree")

    async def discard(self, uri: str) -> None:
        await self._resolve(uri, self._lookup(uri), "Discarded")

    async def _resolve(self, uri: str, kept: KeptWorktree, text: str) -> None:
        await discard_worktree(kept.worktree)
        self._kept.pop(uri, None)
        resolved = dataclasses.replace(kept.changeset, operations=(), description=text)
        await kept.publisher.changes_published(resolved, [], chat=kept.chat)


def register_worktree_operations(host: Host, changesets: WorktreeChangesets) -> None:
    """Make Apply and Discard invocable on *host*; operations are the embedder's to register."""
    host.register_operation(APPLY_OPERATION, lambda uri, _op, _target: changesets.apply(uri))
    host.register_operation(DISCARD_OPERATION, lambda uri, _op, _target: changesets.discard(uri))
    changesets.operations_registered = True
