"""Real edits in a real git working tree.

The demo used to invent a directory under `examples/` and copy files around in
it. That taught the wrong lesson twice over: it published *proposals* for files
that were never written (a changeset is a RECORD, so clients could not open
them), and its "operations" were no-ops, so the buttons looked broken.

This works on the directory the host actually serves. The agent edits files
there for real, and `git` is both the source of truth for "before" and the
independent check on whether the Changes view is telling the truth --
`git diff` in that directory must agree with what the client renders.

**Git is the demo's, not the library's.** Nothing in `core/` shells out; a host
that wraps a workspace with no git in it still works, and this module simply
reports no baseline. That keeps the anti-goal ("a requirement that every
workspace has a local filesystem or Git repository") intact for the library
while letting the demo be a full exploration.

Blast radius: every path this module writes is inside the served root, and the
operations that undo things (`revert`) are scoped to the specific files the
demo manages rather than to the whole tree -- a blanket `git checkout -- .`
would eat work the demo did not create.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent_host_server.core.changesets import Changeset, ChangesetOperation, FileChange

__all__ = [
    "WORKSPACE_OPERATIONS",
    "DemoWorkspace",
    "changeset_uris",
    "publish_workspace_changesets",
    "workspace_changeset",
]

_log = logging.getLogger(__name__)

#: Seconds any single git call may take. A hung git (a lock, a credential
#: prompt on a misconfigured remote) must not hang a turn.
_GIT_TIMEOUT = 15.0

WORKSPACE_OPERATIONS = (
    ChangesetOperation(
        id="ahs-stage",
        label="Stage all",
        description="git add -- <the files in this changeset>",
        scopes=("changeset", "resource"),
        icon="add",
        group="1_git",
    ),
    ChangesetOperation(
        id="ahs-commit",
        label="Commit",
        description="git commit the staged changes, with a generated message",
        scopes=("changeset",),
        icon="check",
        group="1_git",
    ),
    ChangesetOperation(
        id="ahs-revert",
        label="Revert",
        description="Restore these files from HEAD and delete the ones the agent created.",
        scopes=("changeset", "resource"),
        icon="discard",
        group="2_undo",
        # It really does destroy the agent's work now, so it really does ask.
        confirmation="Discard the agent's edits and restore these files from git?",
    ),
    ChangesetOperation(
        id="ahs-review",
        label="Mark all reviewed",
        description="Ticks every Viewed box. Changes no files.",
        scopes=("changeset",),
        icon="eye",
        group="3_review",
    ),
)


def changeset_uris(session_uri: str) -> tuple[str, str]:
    """The two changeset channels for one session: (uncommitted, staged).

    Stable WITHIN a session, so republishing replaces the changeset rather
    than appending a new one with a fresh random URI on every turn -- and
    distinct ACROSS sessions, because a channel is registered globally and two
    sessions sharing a constant URI collide on the second one. That collision
    is not theoretical: fixed constants here made the second session's publish
    raise `channel already registered`, which killed the turn AFTER the files
    had been edited, so the tree changed and the Changes view stayed empty.
    """
    token = hashlib.sha256(session_uri.encode()).hexdigest()[:12]
    return f"ahp-changeset:/{token}-uncommitted", f"ahp-changeset:/{token}-staged"


def workspace_changeset(root: Path, label: str, uri: str) -> Changeset:
    return Changeset(
        uri=uri,
        label=label,
        description=f"Real working-tree edits under {root}.",
        # These ARE uncommitted working-tree edits. Saying `session` would be
        # a less precise claim about the same bytes.
        change_kind="uncommitted",
        reviewable=True,
        operations=WORKSPACE_OPERATIONS,
    )


class DemoWorkspace:
    """The served directory, as something the demo agent can work in."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._git = shutil.which("git")

    # ─── git, best effort ────────────────────────────────────────────────

    @property
    def is_git(self) -> bool:
        return self._git is not None and (self.root / ".git").exists()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run one git command in the root. Never raises for a non-zero exit.

        The caller decides what a failure means -- `git add` on an unchanged
        file is fine, `git commit` with nothing staged is fine, and turning
        either into an exception would make an operation report an error for
        doing nothing wrong.
        """
        if self._git is None:
            raise RuntimeError("git is not installed")
        return subprocess.run(
            [self._git, *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )

    def head_bytes(self, relative: str) -> bytes | None:
        """The file as HEAD has it, or ``None`` if git does not know it.

        This is what makes `before` honest: the changeset's "before" is the
        committed state, not something the demo remembered.
        """
        if not self.is_git:
            return None
        result = subprocess.run(
            [str(self._git), "show", f"HEAD:{relative}"],
            cwd=self.root,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None

    def status(self) -> str:
        return self.git("status", "--short").stdout if self.is_git else ""

    # ─── the work ────────────────────────────────────────────────────────

    #: Paths the demo manages, relative to the root. Everything it writes,
    #: deletes or reverts is in here -- an operation that reached beyond this
    #: list could eat work the demo did not create.
    MANAGED = ("src/greeter/core.py", "config.json", "docs/notes.md", "NOTES-FROM-AGENT.md")

    def apply_demo_edits(self, message: str) -> list[FileChange]:
        """Edit, delete and create -- for real -- and report what happened.

        All three diff shapes, because a demo that only shows edits teaches
        nothing about the other two.
        """
        changes: list[FileChange] = []

        core = self.root / "src/greeter/core.py"
        if core.is_file():
            before = self.head_bytes("src/greeter/core.py") or core.read_bytes()
            text = core.read_text()
            greeting = message.strip().replace('"', "'") or "hello"
            after_text = _replace_assignment(text, "GREETING", f'"{greeting}"')
            after_text = _replace_assignment(after_text, "RETRIES", "3")
            core.write_text(after_text)
            changes.append(FileChange(uri=core.as_uri(), before=before, after=after_text.encode()))

        config = self.root / "config.json"
        if config.is_file():
            before = self.head_bytes("config.json") or config.read_bytes()
            try:
                parsed = json.loads(config.read_bytes() or b"{}")
                parsed["retries"] = 3
                parsed["verbose"] = True
                after = (json.dumps(parsed, indent=2) + "\n").encode()
            except ValueError:
                after = config.read_bytes()
            config.write_bytes(after)
            changes.append(FileChange(uri=config.as_uri(), before=before, after=after))

        notes = self.root / "docs/notes.md"
        before_notes = self.head_bytes("docs/notes.md")
        if notes.is_file():
            before_notes = before_notes or notes.read_bytes()
            notes.unlink()
            changes.append(FileChange(uri=notes.as_uri(), before=before_notes))

        created = self.root / "NOTES-FROM-AGENT.md"
        body = f"# Notes from the agent\n\nAsked: {message.strip()}\n"
        created.write_text(body)
        # `before=None` is a creation. The file really is on disk now, which is
        # what lets a client open it -- publishing a creation for a file that
        # does not exist is how "the file was not found" happens.
        changes.append(FileChange(uri=created.as_uri(), after=body.encode()))

        return changes

    # ─── reading the tree, rather than remembering what we did ───────────

    def _show(self, ref: str, relative: str) -> bytes | None:
        """`git show <ref>:<path>`, or None when that ref has no such file."""
        if self._git is None:
            return None
        result = subprocess.run(
            [self._git, "show", f"{ref}:{relative}"],
            cwd=self.root,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None

    def _worktree(self, relative: str) -> bytes | None:
        path = self.root / relative
        try:
            return path.read_bytes()
        except OSError:
            return None

    def porcelain(self) -> list[tuple[str, str, str]]:
        """`(index_status, worktree_status, path)` for everything git reports.

        Read from git rather than remembered, which is the whole point: after
        the user stages or commits, what the client renders has to come from
        the tree, not from what the demo did three turns ago.
        """
        if not self.is_git:
            return []
        rows: list[tuple[str, str, str]] = []
        for line in self.git("status", "--porcelain", "-z").stdout.split("\0"):
            if len(line) < 4:
                continue
            rows.append((line[0], line[1], line[3:]))
        return rows

    def staged_changes(self) -> list[FileChange]:
        """The index against HEAD -- "the staged index", one of the guide's own
        examples of a changeset."""
        changes: list[FileChange] = []
        for index_status, _worktree_status, relative in self.porcelain():
            if index_status in (" ", "?"):
                continue
            uri = (self.root / relative).as_uri()
            before = None if index_status == "A" else self._show("HEAD", relative)
            after = None if index_status == "D" else self._show("", relative)
            changes.append(FileChange(uri=uri, before=before, after=after))
        return changes

    def uncommitted_changes(self) -> list[FileChange]:
        """The working tree against the index: what is NOT yet staged.

        Together with `staged_changes` this makes staging visible -- a file
        moves from one changeset to the other, which is the only feedback the
        client gives, because it discards the invoke result entirely.
        """
        changes: list[FileChange] = []
        for index_status, worktree_status, relative in self.porcelain():
            if worktree_status == " ":
                continue
            uri = (self.root / relative).as_uri()
            untracked = index_status == "?"
            before = None if untracked else self._show("", relative)
            after = None if worktree_status == "D" else self._worktree(relative)
            changes.append(FileChange(uri=uri, before=before, after=after))
        return changes

    # ─── the operations ──────────────────────────────────────────────────

    def _relative(self, uri: str) -> str | None:
        from urllib.parse import unquote, urlparse

        try:
            path = Path(unquote(urlparse(uri).path)).resolve()
            return str(path.relative_to(self.root))
        except (ValueError, OSError):
            return None

    def _targets(self, uris: list[str], target: str | None) -> list[str]:
        """Which managed paths an operation applies to."""
        chosen = [target] if target else uris
        relative = [r for r in (self._relative(u) for u in chosen) if r]
        return [r for r in relative if r in self.MANAGED]

    def stage(self, uris: list[str], target: str | None = None) -> str:
        paths = self._targets(uris, target)
        if not paths or not self.is_git:
            return "nothing to stage"
        # `-A` so a deletion is staged as a deletion rather than skipped.
        result = self.git("add", "-A", "--", *paths)
        return result.stderr.strip() or f"staged {len(paths)} path(s)"

    def commit(self, message: str) -> str:
        if not self.is_git:
            return "not a git repository"
        result = self.git(
            "-c",
            "user.email=demo@agent-host-server.invalid",
            "-c",
            "user.name=AHP Demo Agent",
            "commit",
            "-m",
            message,
        )
        if result.returncode != 0:
            # "nothing to commit" is the common case and is not a failure --
            # raising here would paint an error on a button that did the right
            # thing by doing nothing.
            detail = (result.stdout or result.stderr).strip().splitlines()
            return detail[0] if detail else "nothing to commit"
        return self.git("log", "--oneline", "-1").stdout.strip()

    def revert(self, uris: list[str], target: str | None = None) -> str:
        """Restore managed files from HEAD; delete the ones HEAD never had.

        Scoped to `MANAGED`, never a blanket `git checkout -- .`: the demo must
        not be able to destroy work it did not create.
        """
        paths = self._targets(uris, target)
        if not paths:
            return "nothing to revert"
        restored, removed = [], []
        for relative in paths:
            if self.is_git and self.head_bytes(relative) is not None:
                self.git("checkout", "HEAD", "--", relative)
                restored.append(relative)
            else:
                path = self.root / relative
                if path.is_file():
                    path.unlink()
                    removed.append(relative)
        return f"restored {len(restored)}, removed {len(removed)}"


def _replace_assignment(text: str, name: str, value: str) -> str:
    """Rewrite a top-level `NAME = ...` line. Left alone if absent."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(f"{name} = "):
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = f"{name} = {value}{ending}"
            break
    return "".join(lines)


async def publish_workspace_changesets(
    publisher: Any, workspace: DemoWorkspace, session_uri: str
) -> None:
    """Publish BOTH changesets from what git currently says.

    Two, not one, because "the staged index" is one of the guide's own examples
    of a changeset -- and because it is the only way staging is visible. The
    client DISCARDS the `invokeChangesetOperation` result (it awaits the call
    and assigns nothing), so a message or a followUp on the result would be
    read by nobody. What it does render is the changeset, so the changeset has
    to change.

    Re-published after every operation, from `git status` rather than from what
    the demo remembers doing: commit empties them, revert empties them, and
    staging moves a file from one to the other.
    """
    uncommitted, staged = changeset_uris(session_uri)
    await publisher.changes_published(
        workspace_changeset(workspace.root, "Uncommitted changes", uncommitted),
        workspace.uncommitted_changes(),
    )
    await publisher.changes_published(
        workspace_changeset(workspace.root, "Staged changes", staged),
        workspace.staged_changes(),
    )
