"""What an ACP agent edits, as AHP file changes: per call, and per session.

ACP tool calls carry `diff` content (stable): `path` (absolute), `oldText`
("the original content", null for a new file) and `newText`. Three things are
made of it, each a :class:`~ahp_host.provider.changes.FileChange`:

- a **preview** while the call waits for approval (`ToolConfirmation.edits`),
  so a client can open the diff before anyone says yes;
- the **call's own edit** once it has completed, which the turn sink turns
  into a `fileEdit` result item -- the diff a client renders in the call's row;
- the **session's changeset**: per file, the content before the agent first
  touched it and after its latest successful edit, which is what makes a
  client's Changes view and its +/- counts work.

The schema says both texts are the file's content, and some agents (gemini,
opencode) send exactly that. Others send the edited *fragment* --
Claude Code's ACP adapter maps its `Edit` tool's `old_string`/`new_string`
straight into the diff -- and a changeset that claimed a fragment was the
whole file would show a file shrunk to three lines. So the texts are checked
against the file itself, read from disk (only inside the served folders,
never outside them, and only small files):

- **after** is the file as it is once the call has completed; the agent's
  `newText` only when the file cannot be read.
- **before** is, in order of preference: the file as it was when the call was
  first announced, if that differs from *after* (agents that show the diff
  before asking permission); nothing, for `oldText: null` (a creation); the
  agent's `oldText`, when `newText` is the whole file or the file cannot be
  read; the file with the one occurrence of the fragment `newText` put back to
  `oldText`. A file none of these settles is left out rather than guessed.
- A **preview** reads the same way round, before the write: the file as it
  is, and the file with `oldText` (the whole file, or its one occurrence)
  replaced by `newText`.

Only completed calls count: a diff on a call that failed or was rejected
describes an edit that did not happen.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ahp_host.provider.changes import Changeset, FileChange

from ahp_host_acp.roots import Roots

log = logging.getLogger(__name__)

#: Larger files are not read back from disk; the agent's own text stands in.
MAX_READ: Final = 4 * 1024 * 1024


class _Unreadable:
    """Outside the served folders, too big, or an I/O error."""


UNREADABLE: Final = _Unreadable()
#: What the disk said: content, ``None`` for no such file, or unreadable.
Disk = bytes | None | _Unreadable


@dataclass(frozen=True)
class Diff:
    path: str
    old_text: str | None
    new_text: str


def diffs_of(content: Sequence[Mapping[str, object]]) -> list[Diff]:
    """The `diff` items of an ACP tool call's content."""
    diffs = []
    for item in content:
        if item.get("type") != "diff":
            continue
        path, old, new = item.get("path"), item.get("oldText"), item.get("newText")
        if isinstance(path, str) and path and isinstance(new, str):
            diffs.append(Diff(path, old if isinstance(old, str) else None, new))
    return diffs


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


class SessionEdits:
    """The session's edits so far, and the changeset that shows them."""

    def __init__(self, roots: Roots) -> None:
        self._roots = roots
        #: Content before the agent's first edit; ``None``: it did not exist.
        self._before: dict[Path, bytes | None] = {}
        self._after: dict[Path, bytes] = {}
        #: The disk when a call was announced, for files not yet tracked.
        self._snapshots: dict[Path, Disk] = {}
        #: One changeset for the session's life, refreshed in place: a new URI
        #: per publish would add a changeset rather than update it.
        self.changeset = Changeset(
            label="Session changes",
            description="Files the agent edited in this session.",
            change_kind="session",
            reviewable=True,
        )

    def _path(self, raw: str, cwd: Path | None) -> Path | None:
        path = Path(raw)
        if not path.is_absolute():
            if cwd is None:
                return None
            path = cwd / path
        return path

    def _read(self, path: Path) -> Disk:
        try:
            if not self._roots.contains(path):
                return UNREADABLE
            real = path.resolve()
            if not real.exists():
                return None
            if not real.is_file() or real.stat().st_size > MAX_READ:
                return UNREADABLE
            return real.read_bytes()
        except OSError:
            return UNREADABLE

    def announced(self, diffs: Sequence[Diff], cwd: Path | None) -> None:
        """A call shows diffs: note the files as they are, before it runs."""
        for diff in diffs:
            path = self._path(diff.path, cwd)
            if path is not None and path not in self._snapshots:
                self._snapshots[path] = self._read(path)

    def preview(self, diffs: Sequence[Diff], cwd: Path | None) -> list[FileChange]:
        """What a call that has not run yet would change, for its approval prompt."""
        previews = []
        for diff in diffs:
            path = self._path(diff.path, cwd)
            planned = self._planned(diff, self._read(path)) if path is not None else None
            if path is not None and planned is not None and planned[0] != planned[1]:
                previews.append(FileChange(uri=path.as_uri(), before=planned[0], after=planned[1]))
        return previews

    @staticmethod
    def _planned(diff: Diff, disk: Disk) -> tuple[bytes | None, bytes] | None:
        new = diff.new_text.encode()
        if not isinstance(disk, bytes):
            # Missing or unreadable: the agent's word.
            return (diff.old_text.encode() if diff.old_text is not None else None), new
        if diff.old_text is None:
            return disk, new  # written over whatever is there
        try:
            text = disk.decode("utf-8")
        except UnicodeDecodeError:
            return diff.old_text.encode(), new
        whole, old, fresh = _lf(text), _lf(diff.old_text), _lf(diff.new_text)
        if whole == old:
            return disk, new
        if whole == fresh:
            return diff.old_text.encode(), disk  # already written
        if old and whole.count(old) == 1:
            planned = whole.replace(old, fresh, 1)
            if "\r\n" in text:
                planned = planned.replace("\n", "\r\n")
            return disk, planned.encode()
        return None

    def abandoned(self, diffs: Sequence[Diff], cwd: Path | None) -> None:
        """The call failed: its snapshots describe nothing that happened."""
        for diff in diffs:
            path = self._path(diff.path, cwd)
            if path is not None:
                self._snapshots.pop(path, None)

    def completed(
        self, diffs: Sequence[Diff], cwd: Path | None
    ) -> tuple[list[tuple[Diff, FileChange]], bool]:
        """The call succeeded: fold its edits in.

        Returns the call's own edits, each diff with its file before this
        call and after it, and whether the session's changeset changed.
        """
        edits: list[tuple[Diff, FileChange]] = []
        changed = False
        for diff in diffs:
            path = self._path(diff.path, cwd)
            if path is None:
                continue
            disk = self._read(path)
            after = disk if isinstance(disk, bytes) else diff.new_text.encode()
            snapshot = self._snapshots.pop(path, UNREADABLE)
            before = self._baseline(diff, after, disk, snapshot)
            if isinstance(before, _Unreadable):
                log.debug("cannot tell what %s was before the agent's edit", path)
                if path not in self._before:
                    continue
            else:
                if before != after:
                    edits.append((diff, FileChange(uri=path.as_uri(), before=before, after=after)))
                if path not in self._before:
                    self._before[path] = before
                    changed = True
            if self._after.get(path) != after:
                self._after[path] = after
                changed = True
        return edits, changed

    @staticmethod
    def _baseline(diff: Diff, after: bytes, disk: Disk, snapshot: Disk) -> Disk:
        if not isinstance(snapshot, _Unreadable) and snapshot != after:
            return snapshot
        if diff.old_text is None:
            return None
        old = diff.old_text.encode()
        if not isinstance(disk, bytes):
            return old  # nothing to check against: the agent's word
        try:
            text = disk.decode("utf-8")
        except UnicodeDecodeError:
            return old
        whole, new, fragment = _lf(text), _lf(diff.new_text), _lf(diff.old_text)
        if whole == new:
            return old
        if new and whole.count(new) == 1:
            rebuilt = whole.replace(new, fragment, 1)
            if "\r\n" in text:
                rebuilt = rebuilt.replace("\n", "\r\n")
            return rebuilt.encode()
        return UNREADABLE

    def changes(self) -> list[FileChange]:
        """Every file whose content differs from before the agent touched it."""
        return [
            FileChange(uri=path.as_uri(), before=self._before.get(path), after=after)
            for path, after in self._after.items()
            if self._before.get(path) != after
        ]
