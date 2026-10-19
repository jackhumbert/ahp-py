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
import logging
from asyncio.subprocess import DEVNULL, PIPE
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ahp_host import TurnSink
from ahp_host.provider.base import ReopensToolChats, SessionPublisher
from ahp_host.provider.changes import Changeset, ChangesetOperation, FileChange

from ahp_host_flow.config import ChangesetsConfig
from ahp_host_flow.engine import run_dir

_log = logging.getLogger(__name__)

WORKTREES_DIR: Final = "worktrees"
APPLY_OPERATION: Final = "ahp-host-flow.worktree.apply"
COMMIT_OPERATION: Final = "ahp-host-flow.worktree.commit"
DISCARD_OPERATION: Final = "ahp-host-flow.worktree.discard"
# Discard reasons queued per chat for its next call.
NOTES_KEEP: Final = 20
# Seconds a discard waits for its notification turn, so the chat is free for the next call.
NOTIFY_WAIT: Final = 5.0


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


async def commit_worktree(wt: Worktree, *, branch: str, message: str) -> str:
    """Commit the worktree's changes in it and name *branch* after the commit; its sha."""
    code, _, _ = await _git("check-ref-format", "--branch", branch, cwd=wt.path)
    if code != 0:
        raise WorktreeError(f"invalid branch name {branch!r}")
    code, _, _ = await _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=wt.path)
    if code == 0:
        raise WorktreeError(f"branch {branch!r} already exists")
    await _stage(wt)
    code, _, _ = await _git("diff", "--cached", "--quiet", cwd=wt.path)
    if code == 1:
        code, _, err = await _git_bytes(
            "commit", "-q", "-F", "-", cwd=wt.path, stdin=message.encode("utf-8")
        )
        if code != 0:
            raise WorktreeError(f"git commit failed: {err.strip()}")
    code, out, err = await _git("rev-parse", "HEAD", cwd=wt.path)
    if code != 0:
        raise WorktreeError(f"git rev-parse failed: {err.strip()}")
    sha = out.strip()
    if sha == wt.base:
        raise WorktreeError("nothing to commit")
    code, _, err = await _git("branch", branch, sha, cwd=wt.path)
    if code != 0:
        raise WorktreeError(f"git branch failed: {err.strip()}")
    return sha


async def commit_onto_repo(wt: Worktree, *, message: str) -> str:
    """Apply the worktree's changes to the repository and commit just those paths; the sha."""
    await _stage(wt)
    # --no-renames: a moved file must list both its old path (a deletion) and its new one, or
    # the deletion is applied to the index but left out of the commit below. Committing just
    # these paths (`git commit -- <paths>`) keeps anything the user had staged out of it.
    code, out, err = await _git_bytes(
        "diff", "--cached", "--name-only", "-z", "--no-renames", wt.base, cwd=wt.path
    )
    if code != 0:
        raise WorktreeError(f"git diff failed: {err.strip()}")
    paths = [p.decode("utf-8", errors="replace") for p in out.split(b"\0") if p]
    if not paths:
        raise WorktreeError("nothing to commit")
    patch = await worktree_patch(wt)
    code, _, err = await _git_bytes(
        "apply", "--index", "--binary", "--whitespace=nowarn", "-", cwd=wt.repo, stdin=patch
    )
    if code != 0:
        raise WorktreeError(f"git apply failed: {err.strip()}")
    code, _, err = await _git_bytes(
        "commit", "-q", "-F", "-", "--", *paths, cwd=wt.repo, stdin=message.encode("utf-8")
    )
    if code != 0:
        raise WorktreeError(f"git commit failed: {err.strip()}")
    code, head, err = await _git("rev-parse", "HEAD", cwd=wt.repo)
    if code != 0:
        raise WorktreeError(f"git rev-parse failed: {err.strip()}")
    return head.strip()


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
    session: str
    run_id: str | None
    ordinal: int
    title: str
    #: The default commit subject (already masked); None falls back to the run and step.
    subject: str | None = None


#: The longest default commit subject taken from a prompt's first line.
SUBJECT_MAX: Final = 72


def commit_subject(label: str | None, prompt: str) -> str | None:
    """The default commit subject of an `agent()` call: its label, else its prompt's first line.

    Unlike the worker chat's title this is not ellipsized: a prompt line is cut at
    `SUBJECT_MAX` characters. None when both are empty.
    """
    if isinstance(label, str) and label.strip():
        return label.strip()
    lines = prompt.strip().splitlines()
    first = lines[0].strip() if lines else ""
    return first[:SUBJECT_MAX].rstrip() or None


def _trailer_value(value: str) -> str:
    """*value* fit for a one-line trailer: CR, LF and other control characters removed."""
    return "".join(c for c in value if c.isprintable() or c == " ").strip()


class WorktreeChangesets:
    """Kept worktrees published as changesets on their worker chats: Apply, Commit, Discard.

    The worktree stays until the changeset is resolved: it is the agent's work, so neither
    the end of the run nor the session closing removes it. The held state is in memory, so
    a host restart drops the changeset; `kept_for` hands the entries to the session's resume
    state and `restore` republishes them afterwards. The operations are declared by
    `FlowProvider.changeset_operations`, which sets `operations_registered`
    when a host is built from the provider.
    """

    def __init__(
        self,
        *,
        branch: str = ChangesetsConfig.branch,
        trailer: bool = False,
        mask: Callable[[str], str] = lambda s: s,
    ) -> None:
        #: The branch template Commit creates (`{runId}`, `{ordinal}`), and whether the commit
        #: message names the principal who pressed it.
        self.branch = branch
        self.trailer = trailer
        self._mask = mask
        #: Discard reasons per worker chat, taken by that chat's next call.
        self._notes: dict[str, deque[str]] = {}
        #: Whether a host was built from the provider, which declares the operations
        #: (`FlowProvider.changeset_operations` sets this); before that a changeset has none.
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
        session: str = "",
        run_id: str | None = None,
        subject: str | None = None,
    ) -> str | None:
        """Publish *wt*'s changes on *chat*; the changeset's URI, or None when there are none.

        *subject* is the default commit message (see `commit_subject`); it is masked here,
        before it is held or written to the resume state.
        """
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
                    COMMIT_OPERATION,
                    "Commit",
                    description="Commit this agent's changes on a branch and remove its worktree",
                    icon="git-commit",
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
        self._kept[uri] = KeptWorktree(
            wt,
            publisher,
            chat,
            changeset,
            session,
            run_id,
            ordinal,
            title,
            self._mask(subject) if subject else None,
        )
        return uri

    def kept_for(self, session: str) -> list[dict[str, Any]]:
        """The kept worktrees of *session* as resume-state entries, by (chat, ordinal)."""
        kept = sorted(
            (k for k in self._kept.values() if k.session == session),
            key=lambda k: (k.chat, k.ordinal),
        )
        entries: list[dict[str, Any]] = []
        for k in kept:
            entry: dict[str, Any] = {
                "chat": k.chat,
                "runId": k.run_id,
                "ordinal": k.ordinal,
                "repo": str(k.worktree.repo),
                "path": str(k.worktree.path),
                "cwd": str(k.worktree.cwd),
                "base": k.worktree.base,
                "label": k.title,
            }
            if k.subject:
                entry["subject"] = k.subject
            entries.append(entry)
        return entries

    async def restore(
        self,
        entries: Sequence[Mapping[str, Any]],
        publisher: SessionPublisher,
        *,
        session: str,
    ) -> int:
        """Republish the kept worktrees *entries* name; how many came back.

        The changeset URI is new and its operations work. An entry whose directory is gone
        (or that has no reviewable changes left) is dropped with a log line.
        """
        restored = 0
        for entry in entries:
            chat, run_id, ordinal = entry.get("chat"), entry.get("runId"), entry.get("ordinal")
            repo, path, cwd = entry.get("repo"), entry.get("path"), entry.get("cwd")
            base, label = entry.get("base"), entry.get("label")
            subject = entry.get("subject")
            if not (
                isinstance(chat, str)
                and (run_id is None or isinstance(run_id, str))
                and isinstance(ordinal, int)
                and not isinstance(ordinal, bool)
                and isinstance(repo, str)
                and isinstance(path, str)
                and isinstance(cwd, str)
                and isinstance(base, str)
                and isinstance(label, str)
                and (subject is None or isinstance(subject, str))
            ):
                _log.warning("ignoring a malformed kept worktree entry")
                continue
            if not Path(path).is_dir():
                _log.warning(
                    "dropping kept worktree %s of agent() #%s: its directory is gone",
                    path,
                    ordinal,
                )
                continue
            try:
                uri = await self.publish(
                    Worktree(Path(repo), Path(path), Path(cwd), base),
                    publisher,
                    chat=chat,
                    title=label,
                    ordinal=ordinal,
                    session=session,
                    run_id=run_id,
                    subject=subject,
                )
            except Exception as exc:
                _log.warning("dropping kept worktree %s of agent() #%s: %s", path, ordinal, exc)
                continue
            if uri is None:
                _log.warning(
                    "dropping kept worktree %s of agent() #%s: %s",
                    path,
                    ordinal,
                    "no reviewable changes",
                )
                continue
            restored += 1
        return restored

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

    async def commit(
        self, uri: str, *, params: Mapping[str, Any], principal: str | None
    ) -> dict[str, str]:
        """Commit the kept worktree on a new branch, or onto the working tree (`apply`)."""
        kept = self._lookup(uri)
        message = params.get("message")
        if message is not None and not isinstance(message, str):
            raise WorktreeError("params.message must be a string")
        message = (message or "").strip() or self.default_message(kept)
        onto_repo = params.get("apply", False)
        if not isinstance(onto_repo, bool):
            raise WorktreeError("params.apply must be a boolean")
        signer = _trailer_value(principal) if principal else ""
        if self.trailer and signer:
            # A principal comes from a header or a token; a newline in it would forge trailers.
            message += f"\n\nFlow-Principal: {signer}"
        if onto_repo:
            sha = await commit_onto_repo(kept.worktree, message=message)
            text = f"Committed {sha[:12]} on the working tree"
        else:
            branch = self.branch.replace("{runId}", kept.run_id or "run").replace(
                "{ordinal}", str(kept.ordinal)
            )
            sha = await commit_worktree(kept.worktree, branch=branch, message=message)
            text = f"Committed {sha[:12]} on branch {branch}"
        await self._resolve(uri, kept, text)
        return {"message": text}

    def default_message(self, kept: KeptWorktree) -> str:
        """The commit message when the request names none: the subject, else run and step."""
        if kept.subject:
            return kept.subject
        return self._mask(f"flow: run {kept.run_id or 'run'} step {kept.ordinal}")

    async def discard(self, uri: str, *, reason: str | None = None) -> None:
        kept = self._lookup(uri)
        if not reason:
            await self._resolve(uri, kept, "Discarded")
            return
        masked = self._mask(reason)
        _log.info("agent() #%s: changeset discarded: %s", kept.ordinal, masked)
        await self._resolve(uri, kept, f"Discarded: {masked}")
        self._notes.setdefault(kept.chat, deque(maxlen=NOTES_KEEP)).append(
            f"[discarded: {kept.title}] {masked}"
        )
        await self._notify(kept, masked)

    def take_notes(self, chat: str) -> list[str]:
        """The discard reasons queued for *chat*, oldest first; they are not kept."""
        queue = self._notes.pop(chat, None)
        return list(queue) if queue else []

    async def _notify(self, kept: KeptWorktree, masked: str) -> None:
        """Post the reason as a turn on the worker chat; best effort, never raised."""
        publisher = kept.publisher
        try:
            if not isinstance(publisher, ReopensToolChats):
                return
            chat = publisher.worker_chat(kept.chat)
            if not chat:
                return
            ended = asyncio.Event()

            async def run(sink: TurnSink) -> None:
                await sink.system_notification(f"Discarded: {masked}")

            if await chat.run_turn("Changeset discarded", run, on_end=ended.set):
                await asyncio.wait_for(ended.wait(), NOTIFY_WAIT)
        except Exception as exc:
            _log.warning("agent() #%s: discard notification failed: %s", kept.ordinal, exc)

    async def _resolve(self, uri: str, kept: KeptWorktree, text: str) -> None:
        await discard_worktree(kept.worktree)
        self._kept.pop(uri, None)
        resolved = dataclasses.replace(kept.changeset, operations=(), description=text)
        await kept.publisher.changes_published(resolved, [], chat=kept.chat)
