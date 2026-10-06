"""What Claude's file-editing tools change: before a call runs, as it ends, and in all.

Claude Code edits files with four tools, whose inputs name the file and say
what changes (read from the CLI bundled with the SDK):

* ``Write``: ``file_path``, ``content`` - the whole new file;
* ``Edit``: ``file_path``, ``old_string``, ``new_string``, ``replace_all`` -
  one exact occurrence of ``old_string`` replaced (every one with
  ``replace_all``), and Claude Code refuses an ``old_string`` it finds more
  than once without it; an empty ``old_string`` on a missing file creates it;
* ``MultiEdit``: ``file_path`` and ``edits``, a list of those, applied in order;
* ``NotebookEdit``: ``notebook_path`` and a cell operation.

Three uses, each only inside the served folders and only for files up to
`MAX_BYTES` - a diff of a file the host does not serve is not the host's to
show, and a huge one is not worth the content store:

* **A preview before approval** (`ToolConfirmation.edits`): the file on disk,
  and the edit applied to it. Computed only when it can be computed exactly
  as Claude Code would - an ``old_string`` that is not there, or there more
  than once, gives no preview rather than a wrong one. Not for
  ``NotebookEdit``, whose cell edits are Claude Code's to interpret.
* **The call's own diff** (`TurnSink.file_edit`): the file as it was when the
  call was about to run (the `PreToolUse` hook) and as it is once the call has
  succeeded. Read from disk both times, so it is what happened, whatever tool
  or cell operation made it; nothing for a call that failed or was denied.
* **The chat's changeset** (`SessionPublisher.changes_published`): per file,
  the content before Claude first touched it and after its latest edit.

A shell command that edits a file (``sed -i``, a code generator) is not seen:
nothing says which files it touched, and guessing from the command line would
show the wrong diff as often as the right one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ahp_host.provider.changes import Changeset, FileChange

from ahp_host_claude.roots import Roots

log = logging.getLogger(__name__)

__all__ = ["EDIT_TOOLS", "MAX_BYTES", "Edits", "Target", "preview", "target_of"]

#: Claude Code's file-editing tools.
EDIT_TOOLS: Final = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
#: Larger files get no preview and no diff.
MAX_BYTES: Final = 2 * 1024 * 1024


class _Unreadable:
    """Outside the served folders, too big, not a file, or an I/O error."""


UNREADABLE: Final = _Unreadable()
#: What the disk said: content, ``None`` for no such file, or unreadable.
Disk = bytes | None | _Unreadable


@dataclass(frozen=True)
class Target:
    """The file a call edits: where it really is, and its URI as clients see it."""

    path: Path
    uri: str


def target_of(
    tool_name: str, tool_input: Mapping[str, Any], *, cwd: Path, roots: Roots
) -> Target | None:
    """The served file *tool_input* edits, or None (another tool, or not served)."""
    if tool_name not in EDIT_TOOLS:
        return None
    raw = tool_input.get("notebook_path" if tool_name == "NotebookEdit" else "file_path")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    try:
        real = path.resolve()
    except OSError:
        return None
    if not roots.contains(real):
        return None
    uri = roots.tree_uri(real)
    return Target(path=real, uri=uri) if uri is not None else None


def read(path: Path) -> Disk:
    try:
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return UNREADABLE
        return path.read_bytes()
    except OSError:
        return UNREADABLE


def _replace(text: str, old: str, new: str, every: bool) -> str | None:
    """`Edit`'s replacement, or None where Claude Code would refuse it."""
    count = text.count(old)
    if not old or count == 0 or (count > 1 and not every):
        return None
    return text.replace(old, new) if every else text.replace(old, new, 1)


def _edited(before: bytes | None, edits: Sequence[Mapping[str, Any]]) -> bytes | None:
    """The file after *edits*, or None if any of them cannot be applied exactly."""
    if before is None:
        # Only a creation: an empty `old_string` on a file that is not there.
        if len(edits) != 1 or edits[0].get("old_string") != "":
            return None
        new = edits[0].get("new_string")
        return new.encode() if isinstance(new, str) else None
    try:
        original = before.decode("utf-8")
    except UnicodeDecodeError:
        return None
    crlf = "\r\n" in original
    text = original.replace("\r\n", "\n") if crlf else original
    for edit in edits:
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        replaced = _replace(
            text,
            old.replace("\r\n", "\n"),
            new.replace("\r\n", "\n"),
            edit.get("replace_all") is True,
        )
        if replaced is None:
            return None
        text = replaced
    return (text.replace("\n", "\r\n") if crlf else text).encode()


def preview(
    tool_name: str, tool_input: Mapping[str, Any], *, cwd: Path, roots: Roots
) -> FileChange | None:
    """The change a call is asking to make, or None if it cannot be told exactly."""
    target = target_of(tool_name, tool_input, cwd=cwd, roots=roots)
    if target is None or tool_name == "NotebookEdit":
        return None
    before = read(target.path)
    if isinstance(before, _Unreadable):
        return None
    after: bytes | None
    if tool_name == "Write":
        content = tool_input.get("content")
        after = content.encode() if isinstance(content, str) else None
    elif tool_name == "Edit":
        after = _edited(before, [tool_input])
    else:
        edits = tool_input.get("edits")
        valid = isinstance(edits, Sequence) and all(isinstance(e, Mapping) for e in edits)
        after = _edited(before, list(edits)) if valid and edits else None
    if after is None or len(after) > MAX_BYTES or after == before:
        return None
    return FileChange(uri=target.uri, before=before, after=after)


@dataclass
class _Pending:
    target: Target
    before: Disk


@dataclass
class Edits:
    """One chat's (or session's) edits so far, and the changeset showing them."""

    roots: Roots
    #: One changeset for the scope's life, refreshed in place: a new URI per
    #: publish would add a changeset rather than update it.
    changeset: Changeset = field(
        default_factory=lambda: Changeset(
            label="Claude's changes",
            description="Files Claude edited with its own editing tools.",
            change_kind="session",
            reviewable=True,
        )
    )
    _pending: dict[str, _Pending] = field(default_factory=dict)
    #: By URI: the content before the first edit (``None``: it did not exist),
    #: and after the latest.
    _before: dict[str, bytes | None] = field(default_factory=dict)
    _after: dict[str, bytes | None] = field(default_factory=dict)

    def starting(
        self, call_id: str, tool_name: str, tool_input: Mapping[str, Any], cwd: Path
    ) -> None:
        """A call is about to run: note its file as it is now."""
        if call_id in self._pending:
            return
        target = target_of(tool_name, tool_input, cwd=cwd, roots=self.roots)
        if target is not None:
            self._pending[call_id] = _Pending(target=target, before=read(target.path))

    def finished(self, call_id: str, *, success: bool) -> FileChange | None:
        """The call ended. Its diff, if it succeeded and changed a file it could read."""
        pending = self._pending.pop(call_id, None)
        if pending is None or not success or isinstance(pending.before, _Unreadable):
            return None
        after = read(pending.target.path)
        if isinstance(after, _Unreadable) or after == pending.before:
            return None
        return self.record(FileChange(uri=pending.target.uri, before=pending.before, after=after))

    def record(self, change: FileChange) -> FileChange:
        """Fold one call's change into the totals."""
        if change.uri not in self._before:
            self._before[change.uri] = change.before
        self._after[change.uri] = change.after
        return change

    def changes(self) -> list[FileChange]:
        """Every file whose content differs from before Claude first touched it."""
        return [
            FileChange(uri=uri, before=self._before.get(uri), after=after)
            for uri, after in self._after.items()
            if self._before.get(uri) != after
        ]
