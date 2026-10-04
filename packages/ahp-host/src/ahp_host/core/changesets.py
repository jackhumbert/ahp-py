"""Changesets: what the agent changed, and what a client can do about it.

A changeset is two things at once -- a **catalogue entry** on the session
(`label`, `uriTemplate`, capabilities) and a **channel** carrying the file list.
The catalogue is what a client renders in a list; the channel is what it opens.

Three decisions here are worth stating, because each avoids a trap.

**Content lives in a host-owned store, not on disk.** A diff needs the bytes
*before* the edit, and by the time anyone asks, the file on disk is the bytes
after. So `before`/`after` are `ContentRef`s into :class:`ContentStore`, served
through a scoped `resourceRead` that never touches the filesystem -- exactly how
VS Code serves its own `git-blob:` scheme, and the reason a changeset does not
require the `resource*` family to be enabled at all.

**The URIs are minted here and registered exactly.** `AGENTS.md` invariant 15
forbids routing on a URI scheme, and a changeset channel is only safe to
register lazily if the host can parse the URI it was handed. It never has to:
a variable-free `uriTemplate` "is itself a subscribable URI", so the channel is
registered when the catalogue is published and an exact-string lookup answers
every subscribe. Templates with `{turnId}` would be expanded *by the client*,
with values the host cannot enumerate -- which is why per-turn changesets are
not offered.

**No operation handlers ship with this library.** `commit`, `create-pr`,
`discard-changes` and `sync` are VS Code private string constants, not protocol
names. One is a credentialed network call and one irreversibly destroys work.
The registry is here; what goes in it is the embedder's decision.
"""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from ahp_protocol import errors

from ahp_host.core.resources import ResourceContent
from ahp_host.provider.changes import (
    Changeset,
    ChangesetOperation,
    FileChange,
    OperationHandler,
)

#: The four provider-facing names are re-exported, not defined here. They live
#: in `ahp_host.provider.changes` so a provider can describe a
#: changeset without importing the runtime -- but this is where a reader of the
#: changeset machinery looks for them.
__all__ = [
    "Changeset",
    "ChangesetOperation",
    "ContentStore",
    "FileChange",
    "OperationHandler",
    "changeset_content_uri",
    "diff_counts",
]

#: Content refs the host serves itself. A private scheme, intercepted before
#: any resource provider is consulted -- so a changeset renders on a host that
#: exposes no filesystem whatsoever.
CONTENT_SCHEME = "ahp-changeset-content:"

#: Bytes above which content is offered by reference only. A `ContentRef`
#: already IS a reference, so this only bounds what the store holds in memory.
DEFAULT_MAX_BLOB = 4 * 1024 * 1024


def changeset_content_uri(blob_id: str) -> str:
    return f"{CONTENT_SCHEME}/{blob_id}"


class ContentStore:
    """Before/after bytes, addressed by content hash.

    Hashed rather than counted, so re-publishing an unchanged file does not
    grow the store and two files with identical content cost one copy. The
    store is per-session and dies with it: this is a diff cache, not a
    filesystem, and nothing should come to depend on it outliving the session.
    """

    def __init__(self, *, max_blob: int = DEFAULT_MAX_BLOB) -> None:
        self._blobs: dict[str, bytes] = {}
        self._max_blob = max_blob

    def __len__(self) -> int:
        return len(self._blobs)

    def put(self, data: bytes, *, content_type: str | None = None) -> dict[str, Any]:
        """Store bytes and return the `ContentRef` that names them."""
        if len(data) > self._max_blob:
            # Truncated rather than refused: a client showing "3 MB of 40"
            # beats one showing an error where a diff should be.
            data = data[: self._max_blob]
        blob_id = hashlib.sha256(data).hexdigest()[:32]
        self._blobs[blob_id] = data
        ref: dict[str, Any] = {"uri": changeset_content_uri(blob_id), "sizeHint": len(data)}
        if content_type is not None:
            ref["contentType"] = content_type
        return ref

    def get(self, uri: str) -> ResourceContent:
        blob_id = uri.removeprefix(f"{CONTENT_SCHEME}/")
        data = self._blobs.get(blob_id)
        if data is None:
            raise errors.AhpError(-32008, f"No such content: {uri}")
        return ResourceContent(data=data)

    def owns(self, uri: str) -> bool:
        """Whether this store holds *uri* -- the blob itself, not just the scheme.

        Every session's store shares the scheme, so a scheme test made the
        first session answer for every session's content: with two sessions
        publishing changesets, the second's diffs were refused as missing.
        """
        return (
            uri.startswith(CONTENT_SCHEME) and uri.removeprefix(f"{CONTENT_SCHEME}/") in self._blobs
        )

    def absorb(self, other: ContentStore) -> None:
        """Take every blob *other* holds -- for a chat moving between sessions.

        Copied rather than moved: the source session may still reference the
        same bytes from its own changesets, since identical content is shared.
        """
        self._blobs.update(other._blobs)


def diff_counts(before: bytes | None, after: bytes | None) -> dict[str, int]:
    """Added and removed line counts for one file.

    Binary content -- anything that is not valid UTF-8 -- reports zeroes rather
    than guessing, so a client renders "changed" without a bogus line count.
    """
    try:
        old = before.decode() if before else ""
        new = after.decode() if after else ""
    except UnicodeDecodeError:
        return {"added": 0, "removed": 0}

    additions = deletions = 0
    for line in difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), n=0
    ):
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    # `added`/`removed`, which is what `FileEdit.diff` declares. We sent
    # `additions`/`deletions` -- the names `SessionSummary.changes` uses -- so
    # every file in every changeset rendered +0 -0 in the Changes view and the
    # multi-diff editor. The two structures genuinely differ; do not unify them.
    return {"added": additions, "removed": deletions}


def file_entry(
    change: FileChange, store: ContentStore, *, reviewed: bool = False
) -> dict[str, Any]:
    """One `ChangesetFile`, with its content parked in the store.

    The id is the *destination* URI, or the source for a deletion -- which is
    upstream's "typically `after.uri` (or `before.uri` for deletions)". A rename
    therefore changes the id, and the caller is responsible for removing the old
    entry; that is upstream's model, not a choice made here.
    """
    edit: dict[str, Any] = {}
    if change.before is not None:
        edit["before"] = {
            "uri": change.uri,
            "content": store.put(change.before, content_type=change.content_type),
        }
    if change.after is not None:
        edit["after"] = {
            "uri": change.renamed_to or change.uri,
            "content": store.put(change.after, content_type=change.content_type),
        }
    edit["diff"] = diff_counts(change.before, change.after)

    identity = change.renamed_to or change.uri if change.after is not None else change.uri
    entry: dict[str, Any] = {"id": identity, "edit": edit}
    if reviewed:
        # Carried ACROSS a republish. `changeset/contentChanged` replaces the
        # file list wholesale, so a republish that omitted this silently
        # cleared every Viewed tick -- which is what happened the moment
        # republish-after-every-operation was added so the buttons would
        # visibly do something. The reference host does the same thing,
        # recomputing review state and stamping it onto each file.
        entry["reviewed"] = True
    return entry


def changes_summary(files: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """`SessionSummary.changes` -- the roll-up a session list renders.

    Summed from the per-file diffs the host already computed, so the number in
    the list and the number in the changeset cannot disagree.
    """
    additions = deletions = 0
    for entry in files:
        edit = entry.get("edit")
        diff = edit.get("diff") if isinstance(edit, Mapping) else None
        if isinstance(diff, Mapping):
            # READ `added`/`removed` (FileEdit.diff), EMIT
            # `additions`/`deletions` (SessionSummary.changes). The two
            # structures really do use different names, and renaming the
            # per-file keys without touching this silently zeroed the roll-up
            # in the session list.
            additions += int(diff.get("added") or 0)
            deletions += int(diff.get("removed") or 0)
    return {"files": len(files), "additions": additions, "deletions": deletions}
