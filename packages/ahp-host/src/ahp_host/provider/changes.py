"""The vocabulary a provider uses to describe what the agent changed.

Pure data, deliberately on this side of the layering: a provider describes a
changeset, and the host turns that into a catalogue entry, a channel, and a
content store. `ahp_host.core.changesets` is where that happens, and it
imports these -- never the other way round, or the import contract that keeps
the protocol core independent of the runtime would not hold.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Changeset", "ChangesetOperation", "FileChange", "OperationHandler"]


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
    #: REQUIRED. `idle` | `running` | `error` | `disabled`
    #: (`ChangesetOperationStatus`; there is no `failed`) -- and the cause an
    #: `error` one carries rides on `changeset/operationStatusChanged`'s
    #: `error` info, not here.
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


#: Invoked with the changeset URI, the operation id, and the TARGET the client
#: chose -- `None` for a changeset-scoped operation, otherwise
#: `{"kind": "resource"|"range", "resource": URI, ...}`.
#:
#: The target used to be dropped, which made every `resource`- and
#: `range`-scoped operation useless: the handler was told a button had been
#: pressed but not which file it was pressed on, so a per-file operation could
#: only ever guess. From the outside that looks exactly like a button that does
#: nothing.
#:
#: Anything the handler raises becomes the operation's `error`.
OperationHandler = Callable[[str, str, Mapping[str, Any] | None], Awaitable[None]]


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
