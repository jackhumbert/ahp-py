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
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_host_server.core import errors
from agent_host_server.core.resources import ResourceContent

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
        return uri.startswith(CONTENT_SCHEME)


@dataclass(frozen=True)
class FileChange:
    """One file the agent touched, in provider terms.

    `before` absent means a creation; `after` absent means a deletion; a
    different `uri` on each means a rename. Bytes rather than text, because a
    changeset that silently drops a binary file is worse than one that shows it
    as changed with no diff.
    """

    uri: str
    before: bytes | None = None
    after: bytes | None = None
    #: Where the file ended up, when a rename moved it.
    renamed_to: str | None = None
    content_type: str | None = None


@dataclass(frozen=True)
class ChangesetOperation:
    """A button. What it does is entirely the embedder's business."""

    id: str
    label: str
    description: str | None = None
    #: REQUIRED by the protocol. Where the verb may be invoked:
    #: `changeset` | `resource` | `range`. Omitting it made the client's
    #: operations derived throw, and a derived that throws is swallowed by
    #: `onBugIndicatingError` -- so ONE malformed operation silently discarded
    #: the entire operations list for the changeset and logged an error nobody
    #: was reading.
    scopes: Sequence[str] = ("changeset",)
    #: REQUIRED. `idle` | `running` | `failed`, and the reason a failed one
    #: carries rides on `operationStatusChanged`, not here.
    status: str = "idle"
    #: Rendered as the button's icon and grouping. Both are read by the
    #: shipping client, and both cost nothing to send.
    icon: str | None = None
    group: str | None = None
    #: When set, the client confirms with this text before invoking.
    confirmation: str | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "scopes": list(self.scopes),
            "status": self.status,
        }
        for key, value in (
            ("description", self.description),
            ("icon", self.icon),
            ("group", self.group),
            ("confirmation", self.confirmation),
        ):
            if value is not None:
                wire[key] = value
        return wire


#: Invoked with the changeset URI and the operation id. Anything it raises
#: becomes the operation's `error`.
OperationHandler = Callable[[str, str], Awaitable[None]]


@dataclass
class Changeset:
    """A catalogue entry plus the channel behind it."""

    label: str
    #: Minted by the host, never parsed. A variable-free template "is itself a
    #: subscribable URI", so this doubles as the channel.
    uri: str = field(default_factory=lambda: f"ahp-changeset:/{secrets.token_urlsafe(12)}")
    #: Serialised as `changeKind`, NOT `kind`. The client's Changes-view
    #: builder switches on `changeKind` and pushes nothing for a value it does
    #: not recognise, so `kind` produced an empty view -- no tree, no diff, no
    #: review -- with nothing logged. Required by the spec, and no conformance
    #: fixture could have caught it: the session reducer copies `changesets`
    #: wholesale without inspecting a single key.
    change_kind: str = "session"
    description: str | None = None
    reviewable: bool = False
    operations: Sequence[ChangesetOperation] = ()

    def to_catalogue_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "label": self.label,
            "uriTemplate": self.uri,
            "changeKind": self.change_kind,
        }
        if self.description is not None:
            entry["description"] = self.description
        if self.reviewable:
            # A presence flag: `{}` means supported, absence means not.
            entry["capabilities"] = {"review": {}}
        return entry


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


def file_entry(change: FileChange, store: ContentStore) -> dict[str, Any]:
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
    return {"id": identity, "edit": edit}


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
