"""A changeset the demo can actually show.

`Host.publish_changeset` shipped complete and was reachable only by an embedder
holding the Host -- so nothing in a turn could call it, no session ever
published a changeset, and the whole Changes surface was dead code with passing
tests. This is the caller it was missing.

**Nothing here writes to disk.** A changeset is a proposal: `before` is what is
on the filesystem now, `after` is what the agent suggests, and the host serves
both back as content through `resourceRead`. The files under
`examples/demo-changes/` are real so the diff is real; they are never modified.

The operations -- the buttons the Changes view renders -- are deliberately
inert. They demonstrate the invoke round trip and touch nothing. An operation
named `discard-changes` that genuinely destroyed work would be a poor thing to
put behind a demo flag.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_host_server.core.changesets import Changeset, ChangesetOperation, FileChange

__all__ = ["DEMO_OPERATIONS", "demo_changeset", "demo_file_changes"]

_ROOT = Path(__file__).resolve().parents[3] / "examples" / "demo-changes"

#: The buttons. `scopes` and `status` are REQUIRED -- one malformed operation
#: makes the client's derived throw, and a throwing derived is swallowed, so
#: every operation on the changeset silently disappears together.
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
        description="Demo only. Nothing is written, so nothing is lost.",
        scopes=("changeset",),
        icon="discard",
        group="demo",
        # A confirmation the client renders before invoking. Present because a
        # destructive-SOUNDING verb should prompt even when it is a no-op --
        # the demo is what an adapter author copies.
        confirmation="This demo operation does not modify any files. Continue?",
    ),
)


def demo_changeset(label: str = "Demo changes") -> Changeset:
    return Changeset(
        label=label,
        description="Proposed by the echo agent. Nothing is written to disk.",
        change_kind="session",
        reviewable=True,
        operations=DEMO_OPERATIONS,
    )


def _read(name: str) -> bytes:
    try:
        return (_ROOT / name).read_bytes()
    except OSError:
        # A missing demo file is not worth failing a turn over; an empty
        # `before` simply renders as a creation.
        return b""


def demo_file_changes(message: str) -> list[FileChange]:
    """One edit, one creation, one deletion -- all three shapes a diff has.

    The edit folds the user's own message in, so the diff visibly belongs to
    the turn that produced it rather than being the same every time.
    """
    greeting = _read("greeting.txt")
    edited = greeting.replace(b"hello", message.strip().encode() or b"hello", 1)

    config = _read("config.json")
    try:
        parsed = json.loads(config or b"{}")
        parsed["retries"] = 3
        updated = (json.dumps(parsed, indent=2) + "\n").encode()
    except (ValueError, TypeError):
        updated = config

    return [
        FileChange(uri=(_ROOT / "greeting.txt").as_uri(), before=greeting, after=edited),
        FileChange(
            uri=(_ROOT / "config.json").before if False else (_ROOT / "config.json").as_uri(),
            before=config,
            after=updated,
        ),
        # `after=None` is a deletion; `before=None` would be a creation.
        FileChange(
            uri=(_ROOT / "notes.md").as_uri(), before=b"a line the agent proposes removing\n"
        ),
        FileChange(
            uri=(_ROOT / "new-file.txt").as_uri(), after=b"a file the agent proposes creating\n"
        ),
    ]
