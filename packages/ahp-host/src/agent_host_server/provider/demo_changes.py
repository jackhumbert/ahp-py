"""Changes the demo agent actually makes, so the Changes view is truthful.

`Host.publish_changeset` shipped complete and had no caller, so no session ever
published a changeset and the whole Changes surface was dead code with a green
suite. This is the caller it was missing.

**A changeset is a RECORD, not a proposal.** The guide's own examples are
"uncommitted working-tree edits, the diff between two turns, the cumulative
changes for the whole session, the staged index" -- every one a view of changes
that have already happened. `FileEdit.before` is documented as absent "for
in-place file edits", which only makes sense if `after` is the file on disk. So
a client opens `after.uri` and expects to find a file there; publishing a
changeset for a file that does not exist gets you "The editor could not be
opened because the file was not found."

The first version of this module got that backwards and published proposals
against files that were never written. Hence the split below:

* ``baseline/`` is committed and never modified. It is the "before".
* ``scratch/`` is gitignored. Every turn resets it from the baseline and then
  really does the work -- two edits, a deletion, a creation.

Nothing outside ``scratch/`` is ever written.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent_host_server.core.changesets import Changeset, ChangesetOperation, FileChange

__all__ = ["DEMO_OPERATIONS", "demo_changeset", "demo_file_changes", "reset_scratch"]

_HERE = Path(__file__).resolve().parents[3] / "examples" / "demo-changes"
BASELINE = _HERE / "baseline"
SCRATCH = _HERE / "scratch"

#: The buttons. `scopes` and `status` are REQUIRED -- one malformed operation
#: makes the client's derived throw, and a throwing derived is swallowed, so
#: every operation on the changeset disappears together.
DEMO_OPERATIONS = (
    ChangesetOperation(
        id="ahs-approve",
        label="Approve",
        description="Demo only: flips the operation status and changes nothing.",
        scopes=("changeset",),
        icon="check",
        group="demo",
    ),
    ChangesetOperation(
        id="ahs-annotate",
        label="Annotate file",
        description="Demo only: scoped to a single file, to show per-resource scope.",
        scopes=("resource",),
        icon="comment",
        group="demo",
    ),
    ChangesetOperation(
        id="ahs-reset",
        label="Reset demo changes",
        description="Restores scratch/ from baseline/. Touches nothing else.",
        scopes=("changeset",),
        icon="discard",
        group="demo",
        # Present because a destructive-SOUNDING verb should prompt even when
        # its blast radius is one gitignored directory. The demo is what an
        # adapter author copies.
        confirmation="This restores the demo's scratch directory. Continue?",
    ),
)


def demo_changeset(label: str = "Demo changes") -> Changeset:
    return Changeset(
        label=label,
        description="Real edits under examples/demo-changes/scratch/.",
        # `uncommitted` is the honest kind: these ARE working-tree edits that
        # have happened and are not committed.
        change_kind="uncommitted",
        reviewable=True,
        operations=DEMO_OPERATIONS,
    )


def reset_scratch() -> None:
    """Put ``scratch/`` back to the baseline. Safe to call repeatedly."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for existing in SCRATCH.iterdir():
        if existing.is_file():
            existing.unlink()
    for source in BASELINE.iterdir():
        if source.is_file():
            shutil.copy2(source, SCRATCH / source.name)


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def demo_file_changes(message: str) -> list[FileChange]:
    """Reset, DO the work on disk, and report what was done.

    All three shapes a diff has -- an edit, a deletion, a creation -- because a
    demo that only shows edits teaches nothing about the other two. The user's
    own message goes into the edit, so the diff visibly belongs to the turn
    that produced it.
    """
    reset_scratch()

    greeting = SCRATCH / "greeting.txt"
    config = SCRATCH / "config.json"
    notes = SCRATCH / "notes.md"
    created = SCRATCH / "new-file.txt"

    before_greeting = _read(greeting)
    before_config = _read(config)
    before_notes = _read(notes)

    # ─── the work, actually performed ────────────────────────────────────
    after_greeting = before_greeting.replace(b"hello", message.strip().encode() or b"hello", 1)
    greeting.write_bytes(after_greeting)

    try:
        parsed = json.loads(before_config or b"{}")
        parsed["retries"] = 3
        after_config = (json.dumps(parsed, indent=2) + "\n").encode()
    except (ValueError, TypeError):
        after_config = before_config
    config.write_bytes(after_config)

    notes.unlink(missing_ok=True)

    after_created = f"created by the demo agent, in response to: {message.strip()}\n".encode()
    created.write_bytes(after_created)

    # ─── and reported ────────────────────────────────────────────────────
    return [
        FileChange(uri=greeting.as_uri(), before=before_greeting, after=after_greeting),
        FileChange(uri=config.as_uri(), before=before_config, after=after_config),
        # `after=None` is a deletion; the file really is gone from scratch/.
        FileChange(uri=notes.as_uri(), before=before_notes),
        # `before=None` is a creation; the file really is there now, so the
        # client can open it.
        FileChange(uri=created.as_uri(), after=after_created),
    ]
