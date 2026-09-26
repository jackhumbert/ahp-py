"""Changesets: what the agent changed, and what you may do about it.

Read-mostly, and `docs/plan.md` §1.3 says so: one of the eight `changeset/*`
actions is client-dispatchable and the rest is server push into a reducer that
already runs. So this is a **view** over `SessionState.changesets` and the
changeset channel, plus the two writes -- review and invoke -- with the checks
a caller cannot make from the params alone.

Four of those checks are the reason the module exists.

**Content is not on disk.** ``ChangesetFile.after.uri`` names the *file*;
the bytes live behind ``after.content``, a ``ContentRef`` into a store the host
owns, and a changeset "renders on a host that exposes no filesystem at all".
Reading the file URI therefore reads a different thing -- the file as it is
*now*, if a filesystem is even exposed -- and a diff's ``before`` no longer
exists there by definition. :meth:`Changeset.read` takes the
:class:`FileSide`, never a URI, so the wrong one cannot be passed.

**Review is capability-gated, and the gate moves.** ``capabilities.review``
lives on the session's *catalogue entry*, not in the changeset's own state, and
the host re-reads the current entry when it validates the action -- so this
re-reads it too, on every call, rather than remembering what it said at open
time.

**An operation must be one this changeset declared, in a scope it declared.**
The host rejects both with `-32602`, and learning that a `range` operation does
not take a range from a JSON-RPC error is exactly what a typed API should spare
you. The declaration is per-changeset and *changes*: the sibling host recomputes
the list from `git status` on every publish, so "Commit" is simply absent while
nothing is staged.

**A ``confirmation`` is a client MUST.** "The client MUST display this message
to the user ... and only invoke the operation after the user accepts." An
operation carrying one is destructive by declaration -- the sibling host's
``Revert`` discards the agent's edits -- so :meth:`Changeset.invoke` refuses
until the caller passes ``confirmed=True``, which is the only way a library can
make that MUST visible rather than silently unmet.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import quote

from ahp_protocol.types import JsonObject

from ahp_client.client import actions
from ahp_client.client.errors import AhpClientError, InvalidArgument, RequestTimeout
from ahp_client.client.events import ActionEvent

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ahp_client.api.client import Client, Session

__all__ = [
    "KNOWN_TEMPLATE_VARIABLES",
    "WIRE_ENCODINGS",
    "Changeset",
    "ChangesetFile",
    "ChangesetInfo",
    "ChangesetOperation",
    "FileSide",
    "changeset_catalogue",
    "text_range",
]

#: The RFC 6570 variables this protocol version defines. "Any other variable
#: name MUST be ignored by clients (there is no protocol-defined way to obtain
#: values for unknown variables)", which is why an unknown one makes an entry
#: un-openable rather than merely unexpanded.
_TURN: Final = "turnId"
_ORIGINAL: Final = "originalTurnId"
_MODIFIED: Final = "modifiedTurnId"
KNOWN_TEMPLATE_VARIABLES: Final[frozenset[str]] = frozenset({_TURN, _ORIGINAL, _MODIFIED})

#: ``ChangesetOperationTarget.side`` -- a closed enum on both target branches.
_SIDES: Final[frozenset[str]] = frozenset({"before", "after"})

#: ``ChangesetStatus`` values that mean the computation stopped. ``""`` is not
#: one of them: it is what a channel with no state reads as, and treating it as
#: settled is how a dead subscription came to look like an empty diff.
_SETTLED: Final[frozenset[str]] = frozenset({"ready", "error"})

#: ``ContentEncoding`` -- a closed pair, and the only values `resourceRead` may
#: carry. A Python codec name that is not one of these is a local decoding
#: choice; putting it on the wire would be an undeclared enum value.
WIRE_ENCODINGS: Final[frozenset[str]] = frozenset({"base64", "utf-8"})

#: **Every** brace group, not just the ones this build understands. Matching
#: `\{([A-Za-z0-9_]+)\}` instead made an entire class of template invisible: an
#: operator (`{+turnId}`, `{?turnId}`, `{/turnId}`), a dotted name (`{turn.id}`)
#: or a comma list (`{originalTurnId,modifiedTurnId}` -- a legal RFC 6570
#: spelling of the pair the schema calls a MUST) matched nothing, so `variables`
#: was empty, `openable` was True, and `expand` returned the template *with its
#: braces*. Subscribing that succeeds -- the host accepts any channel string --
#: and yields a bound, permanently empty channel with no error anywhere, which
#: is the silent freeze invariant 1 exists to prevent, arriving through the
#: derived-URI door instead of the scheme door.
_BRACE_GROUP = re.compile(r"\{[^{}]*\}")

#: The subset of the above this build can expand: simple string expansion of a
#: bare name. Deliberately not a general 6570 engine -- an operator this protocol
#: never mints is a shape we would be guessing at -- but the guess is now
#: refused rather than made.
_SIMPLE_NAME = re.compile(r"^\{([A-Za-z0-9_]+)\}$")


def _mapping(value: Any) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def text_range(
    start_line: int, start_character: int, end_line: int, end_character: int
) -> JsonObject:
    """A `TextRange` for a ``range``-scoped :meth:`Changeset.invoke`.

    Both positions are zero-based. Unlike ``completions``, whose offsets the
    schema pins to UTF-16 code units, ``TextPosition`` says only "zero-based
    character offset within the line" -- so nothing is converted here, and a
    caller working in UTF-16 units passes them through unchanged.
    """
    return {
        "start": {"line": start_line, "character": start_character},
        "end": {"line": end_line, "character": end_character},
    }


# ── catalogue ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ChangesetInfo:
    """One ``Changeset`` catalogue entry, from ``SessionState.changesets``.

    "Intentionally lightweight -- just enough to render a chip or list row
    without subscribing." The file list is on the channel, not here.
    """

    raw: JsonObject

    @property
    def label(self) -> str:
        return _string(self.raw.get("label"))

    @property
    def uri_template(self) -> str:
        return _string(self.raw.get("uriTemplate"))

    @property
    def change_kind(self) -> str:
        """``session`` | ``branch`` | ``uncommitted`` | ``turn`` |
        ``compare-turns``, advisory -- "clients SHOULD fall back to a reasonable
        default when an unknown value is encountered", so this is never matched
        exhaustively here."""
        return _string(self.raw.get("changeKind"))

    @property
    def description(self) -> str:
        return _string(self.raw.get("description"))

    @property
    def capabilities(self) -> JsonObject:
        return _mapping(self.raw.get("capabilities"))

    @property
    def reviewable(self) -> bool:
        """Whether ``changeset/filesReviewChanged`` may be dispatched at all.

        A **presence flag**: ``{"review": {}}`` means supported and ``{}`` is
        falsy in Python, so the test is ``is not None``. The same trap as
        ``capabilities.multipleChats``, and it bites the same way -- a
        truthiness test reads every reviewable changeset as non-reviewable.
        """
        return self.capabilities.get("review") is not None

    @property
    def variables(self) -> frozenset[str]:
        """The simple `{name}` variables in :attr:`uri_template`.

        A set, so unordered -- the template's order is in
        :attr:`uri_template` itself and nothing here needs it. Brace groups this
        build cannot expand are **not** in here: see :attr:`unexpandable`, which
        is what makes them refusals rather than silent passthroughs.
        """
        return frozenset(
            match.group(1)
            for group in _BRACE_GROUP.findall(self.uri_template)
            if (match := _SIMPLE_NAME.match(group))
        )

    @property
    def unexpandable(self) -> Sequence[str]:
        """Brace groups :meth:`expand` refuses: an operator, a dotted or dashed
        name, a comma list, or a bare name this protocol version does not
        define. "Any other variable name MUST be ignored by clients (there is no
        protocol-defined way to obtain values for unknown variables)" -- and an
        RFC 6570 *operator* is the same problem wearing a different hat, because
        expanding it wrong mints a channel the host never registered."""
        return [
            group
            for group in _BRACE_GROUP.findall(self.uri_template)
            if not (
                (match := _SIMPLE_NAME.match(group)) and match.group(1) in KNOWN_TEMPLATE_VARIABLES
            )
        ]

    @property
    def openable(self) -> bool:
        """Whether :meth:`expand` can produce a subscribable URI at all.

        False for a template carrying anything :attr:`unexpandable` names, and
        for a turn-comparison template missing half its pair, which the schema
        states as a MUST.
        """
        if self.unexpandable:
            return False
        found = self.variables
        return (_ORIGINAL in found) == (_MODIFIED in found)

    def expand(
        self,
        *,
        turn_id: str | None = None,
        original_turn_id: str | None = None,
        modified_turn_id: str | None = None,
    ) -> str:
        """The subscribable URI for this entry.

        A variable-free template "is itself a subscribable URI", which is the
        shape every real host mints today -- so the common case supplies
        nothing and gets the template back.

        Values are percent-encoded to RFC 6570's *unreserved* set, which is what
        simple string expansion specifies; a turn id containing a `/` would
        otherwise silently produce a different channel than the host registered.
        """
        supplied = {_TURN: turn_id, _ORIGINAL: original_turn_id, _MODIFIED: modified_turn_id}
        found = self.variables
        unexpandable = self.unexpandable
        if unexpandable:
            raise AhpClientError(
                f"changeset template {self.uri_template!r} contains {unexpandable}, which this "
                "protocol version does not define a value or an expansion for; clients MUST "
                "ignore such an entry rather than subscribe a URI they guessed at"
            )
        if (_ORIGINAL in found) != (_MODIFIED in found):
            raise AhpClientError(
                f"changeset template {self.uri_template!r} names one of "
                f"{_ORIGINAL}/{_MODIFIED}; the schema requires both or neither"
            )
        missing = sorted(name for name in found if not supplied.get(name))
        if missing:
            raise InvalidArgument(
                f"changeset template {self.uri_template!r} needs {missing}; pass them to expand()"
            )
        # Refused rather than dropped. A value with no slot means the caller
        # believes it is opening a per-turn slice and is in fact opening the
        # session-wide one -- a wrong answer where every other branch here is a
        # refusal, and indistinguishable from success at the call site.
        spare = sorted(name for name, value in supplied.items() if value and name not in found)
        if spare:
            raise InvalidArgument(
                f"changeset template {self.uri_template!r} has no {spare} to substitute; "
                "passing one would silently open a different changeset than you asked for"
            )
        return _BRACE_GROUP.sub(
            lambda m: quote(str(supplied[m.group(0)[1:-1]]), safe=""), self.uri_template
        )

    def matches(self, uri: str) -> bool:
        """Whether *uri* could be an expansion of this entry's template.

        The identity of a catalogue entry is its ``uriTemplate``, but a caller
        holding only an expanded URI -- ``open_changeset("ahp-changeset:/turn/t1")``,
        or one carried across a process boundary -- has no template to match on,
        and the host would accept that subscribe perfectly happily. Without this
        the entry is never found, so :attr:`Changeset.info` is ``None``, and
        review is refused with an error telling the caller to do the thing they
        just did.

        A variable expands to a percent-encoded value, so it can never contain
        ``/``, ``?`` or ``#`` -- which is what keeps this from matching across a
        path segment it was not meant to.
        """
        template = self.uri_template
        if not template:
            return False
        pattern = ""
        cursor = 0
        for group in _BRACE_GROUP.finditer(template):
            pattern += re.escape(template[cursor : group.start()]) + "[^/?#]*"
            cursor = group.end()
        pattern += re.escape(template[cursor:])
        return re.fullmatch(pattern, uri) is not None


# ── the file list ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FileSide:
    """One side of a ``FileEdit``: where the file was, and where its bytes are.

    :attr:`uri` is the file. :attr:`content` is a ``ContentRef`` into the host's
    own store, and it is the only one of the two that reads back the bytes this
    changeset is describing -- see :meth:`Changeset.read`.
    """

    raw: JsonObject

    @property
    def uri(self) -> str:
        return _string(self.raw.get("uri"))

    @property
    def content(self) -> JsonObject:
        return _mapping(self.raw.get("content"))

    @property
    def content_uri(self) -> str:
        return _string(self.content.get("uri"))

    @property
    def size_hint(self) -> int | None:
        """``ContentRef.sizeHint``, a JSON ``number`` -- the same declared type
        as ``diff.added``, which is read here with ``int | float``. Two guards
        for one type in one file meant a legal ``sizeHint: 41.5`` read as
        absent."""
        raw = self.content.get("sizeHint")
        return int(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else None

    @property
    def content_type(self) -> str:
        return _string(self.content.get("contentType"))


@dataclass(frozen=True, slots=True)
class ChangesetFile:
    """One ``ChangesetFile``."""

    raw: JsonObject

    @property
    def id(self) -> str:
        """Stable within the changeset, and the id
        :meth:`Changeset.mark_reviewed` and ``changeset/fileRemoved`` target.

        "Typically ``after.uri`` (or ``before.uri`` for deletions)" -- so a
        rename *changes* the id, and it is not a durable handle to a path.
        """
        return _string(self.raw.get("id"))

    @property
    def edit(self) -> JsonObject:
        return _mapping(self.raw.get("edit"))

    @property
    def before(self) -> FileSide | None:
        """ "Absent for file creations **or for in-place file edits**" -- so its
        absence is not a discriminator on its own. See :attr:`change`."""
        side = self.edit.get("before")
        return FileSide(side) if isinstance(side, dict) else None

    @property
    def after(self) -> FileSide | None:
        """Absent for a deletion."""
        side = self.edit.get("after")
        return FileSide(side) if isinstance(side, dict) else None

    @property
    def change(self) -> str:
        """``created`` | ``deleted`` | ``renamed`` | ``modified`` | ``unknown``.

        Derived, because the protocol carries **no discriminator**, and the
        derivation is deliberately not confident. `FileEdit.before` is "absent
        for file creations *or for in-place file edits*", so a missing `before`
        does not mean a creation: a ``{after, diff: {added: 2, removed: 2}}``
        entry is a legal, named shape that reads as a brand-new file, after
        which :meth:`Changeset.read` on ``before`` returns ``None`` and the UI
        renders "new file, no diff available" for a two-line edit.

        ``diff.removed`` is the only corroboration available, so it is used:
        removed lines are evidence against a creation. It is still a heuristic,
        which is why the answer for an edit carrying neither side is
        ``unknown`` rather than a fifth guess. The sibling host always sends
        both sides, which is exactly why nothing driving it would notice.
        """
        before, after = self.before, self.after
        if before is None and after is None:
            return "unknown"
        if before is None:
            return "modified" if self.removed > 0 else "created"
        if after is None:
            return "deleted"
        return "renamed" if before.uri != after.uri else "modified"

    @property
    def added(self) -> int:
        """``FileEdit.diff.added``. Note the key: ``SessionSummary.changes``
        spells the same quantity ``additions``, and the two structures really do
        differ."""
        return _count(self.edit.get("diff"), "added")

    @property
    def removed(self) -> int:
        return _count(self.edit.get("diff"), "removed")

    @property
    def reviewed(self) -> bool:
        """ "Absent is equivalent to `false` -- clients MUST treat a missing
        value as not-yet-reviewed.\""""
        return self.raw.get("reviewed") is True

    @property
    def meta(self) -> JsonObject:
        """``_meta`` -- "server-defined opaque metadata ... not interpreted by
        the protocol", preserved verbatim."""
        return _mapping(self.raw.get("_meta"))


def _count(diff: Any, key: str) -> int:
    raw = _mapping(diff).get(key)
    return int(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else 0


# ── operations ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ChangesetOperation:
    """A ``ChangesetOperation`` -- a server-declared verb, with its status.

    "The term 'operation' is used deliberately to avoid colliding with the
    protocol-level Actions that mutate state": invoking one is a request, and
    what it does comes back as ordinary `changeset/*` actions.
    """

    raw: JsonObject

    @property
    def id(self) -> str:
        return _string(self.raw.get("id"))

    @property
    def label(self) -> str:
        return _string(self.raw.get("label"))

    @property
    def description(self) -> str:
        return _string(self.raw.get("description"))

    @property
    def icon(self) -> str:
        return _string(self.raw.get("icon"))

    @property
    def group(self) -> str:
        return _string(self.raw.get("group"))

    @property
    def scopes(self) -> frozenset[str]:
        """Where this operation may be invoked; a subset of
        ``{changeset, resource, range}``."""
        raw = self.raw.get("scopes")
        return (
            frozenset(s for s in raw if isinstance(s, str))
            if isinstance(raw, list)
            else frozenset()
        )

    @property
    def status(self) -> str:
        """``idle`` | ``running`` | ``error`` | ``disabled``, verbatim."""
        return _string(self.raw.get("status"))

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def failed(self) -> bool:
        """The wire value is ``"error"``; the most recent invocation failed and
        :attr:`error` says why."""
        return self.status == "error"

    @property
    def disabled(self) -> bool:
        return self.status == "disabled"

    @property
    def error(self) -> JsonObject | None:
        """``ErrorInfo``, "present iff `status === Error`"."""
        raw = self.raw.get("error")
        return raw if isinstance(raw, dict) else None

    @property
    def confirmation(self) -> str | JsonObject | None:
        """``StringOrMarkdown``, verbatim -- a plain string renders as-is and
        ``{"markdown": …}`` renders as Markdown, and flattening the two would
        lose which."""
        raw = self.raw.get("confirmation")
        return raw if isinstance(raw, str | dict) else None

    @property
    def confirmation_text(self) -> str:
        """The prompt to show, flattened. Empty when there is none."""
        prompt = self.confirmation
        if isinstance(prompt, dict):
            return _string(prompt.get("markdown"))
        return prompt or ""

    @property
    def destructive(self) -> bool:
        """Whether the operation declared itself destructive.

        "The presence of this field also signals that the operation is
        destructive", which is why :meth:`Changeset.invoke` will not send one
        unconfirmed -- there is no separate flag to read.
        """
        return self.confirmation is not None


# ── the channel ──────────────────────────────────────────────────────────────


class Changeset:
    """One subscribed changeset channel."""

    def __init__(self, client: Client, session: Session, uri: str, *, template: str) -> None:
        self._client = client
        self._session = session
        self.uri = uri
        #: The catalogue entry's ``uriTemplate``, kept because it is the entry's
        #: identity and :attr:`uri` is not: an expanded `{turnId}` URI matches no
        #: entry, so a `Changeset` that remembered only its channel could never
        #: find the capabilities that gate review.
        self._template = template
        self._closed = False
        #: Readers handed out by :meth:`changes` and :meth:`wait_until_ready`,
        #: so :meth:`aclose` can end them. Without this a `changes()` loop
        #: outlives the subscription it is reporting on -- forever, because it
        #: reads the connection-wide tap and only the connection ends it.
        self._readers: set[Any] = set()

    def __repr__(self) -> str:
        live = "" if self.live else " closed"
        return f"<Changeset {self.uri}{live} status={self.status!r} files={len(self.files())}>"

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def live(self) -> bool:
        """Whether this client is still subscribed to the changeset's channel.

        ``False`` after :meth:`aclose`, and for a URI the host registered
        nothing for. It exists because every read on a dropped channel answers
        with a *plausible* value -- no files, no error, no ``computing`` -- and
        "the agent changed nothing" is the wrong conclusion to hand a caller
        silently.
        """
        return not self._closed and self._client._runtime.subscribed(self.uri)

    @property
    def state(self) -> JsonObject:
        state = self._client.mirror.state(self.uri)
        return state if isinstance(state, dict) else {}

    @property
    def info(self) -> ChangesetInfo | None:
        """The catalogue entry, **re-read every time**.

        Not cached at open time on purpose. The entry is the only copy of the
        label, the description and ``capabilities.review``, the host validates
        review against the *current* one, and the sibling host re-emits the
        entry whenever it changes -- so a changeset that stopped being
        reviewable must stop offering review here in the same breath.

        Matched on ``uriTemplate`` first, then on
        :meth:`ChangesetInfo.matches`: a caller who opened the changeset by its
        already-expanded URI has no template to match on, and refusing them
        review over that made the documented escape hatch -- the one the spec's
        "subscribe to what comes back" advice relies on -- second-class.
        """
        entries = list(self._session.changesets())
        for entry in entries:
            if entry.uri_template == self._template:
                return entry
        return next((entry for entry in entries if entry.matches(self.uri)), None)

    @property
    def status(self) -> str:
        """``computing`` | ``ready`` | ``error``."""
        return _string(self.state.get("status"))

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def failed(self) -> bool:
        return self.status == "error"

    @property
    def error(self) -> JsonObject | None:
        """``ErrorInfo``, "present iff `status === Error`"."""
        raw = self.state.get("error")
        return raw if isinstance(raw, dict) else None

    def files(self) -> Sequence[ChangesetFile]:
        raw = self.state.get("files")
        return (
            [ChangesetFile(f) for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []
        )

    def file(self, file_id: str) -> ChangesetFile | None:
        return next((f for f in self.files() if f.id == file_id), None)

    def operations(self) -> Sequence[ChangesetOperation]:
        """What may be invoked **right now**.

        Recomputed by the host and re-published: the sibling derives this list
        from `git status` on every publish, so "Commit" is absent while nothing
        is staged rather than present-and-failing.
        """
        raw = self.state.get("operations")
        return (
            [ChangesetOperation(o) for o in raw if isinstance(o, dict)]
            if isinstance(raw, list)
            else []
        )

    def operation(self, operation_id: str) -> ChangesetOperation | None:
        return next((o for o in self.operations() if o.id == operation_id), None)

    # ── content ──────────────────────────────────────────────────────────────

    async def read(self, side: FileSide | None) -> bytes | None:
        """The bytes behind one side of a file edit. ``None`` for a side that
        does not exist -- a creation has no ``before``, a deletion no ``after``.

        Takes the :class:`FileSide` rather than a URI **so the file URI cannot
        be passed by mistake**, which is the mistake this whole surface exists
        to prevent: the bytes live in a store the host owns, addressed by
        ``ContentRef``, and a diff's ``before`` no longer exists on disk by the
        time anyone asks for it. A changeset renders on a host that exposes no
        filesystem at all, so reading ``side.uri`` may simply have no answer.

        Sent as ``resourceRead`` on the **root** channel, which is what
        ``ResourceReadParams`` declares (`channel: 'ahp-root://'`). The scoping
        that matters is not the channel -- it is that the URI is the content
        ref, which the receiver resolves out of its own store before any
        resource provider is consulted.
        """
        if side is None:
            return None
        if not side.content_uri:
            raise AhpClientError(
                f"{side.uri or 'this file side'} carries no ContentRef; there is nothing to read"
            )
        return await self.read_content(side.content)

    async def read_content(self, ref: Mapping[str, Any], *, encoding: str | None = None) -> bytes:
        """The bytes behind any ``ContentRef`` on this surface.

        :meth:`read` covers the file sides, which is where the wrong-URI mistake
        lives. This covers the *other* one: ``InvokeChangesetOperationResult``
        carries an optional ``followUp.content``, a bare ``ContentRef`` the
        client is expected to fetch and show -- the one output artefact of this
        surface's own write path. Without a public entry point a caller drops to
        ``client.protocol.resource_read`` **and** re-implements the base64/utf-8
        branch, which is the exact hand-assembly this module exists to prevent.

        *encoding* is a request, not a promise: ``ContentEncoding`` is the closed
        pair ``base64`` / ``utf-8``, the server "SHOULD honor" it and "MUST fall
        back to either" if it cannot, so the reply is decoded from whatever
        actually came back. Anything outside that pair is a *local* decoding
        preference and is not put on the wire, where it would be an undeclared
        enum value.
        """
        uri = _string(ref.get("uri"))
        if not uri:
            raise InvalidArgument("a ContentRef needs a uri; there is nothing to read")
        extra = {"encoding": encoding} if encoding in WIRE_ENCODINGS else {}
        result = await self._client.protocol.resource_read(uri, **extra)
        return _decode(result)

    async def read_text(self, side: FileSide | None, *, encoding: str = "utf-8") -> str | None:
        """:meth:`read`, decoded. Raises on bytes that are not text in
        *encoding* rather than replacing characters -- a mangled diff is worse
        than a refusal.

        *encoding* is also sent as ``ResourceReadParams.encoding``, which the
        server SHOULD honour: this call knows it wants text, and saying so is
        the difference between reading a diff and base64-decoding one.
        """
        if side is None:
            return None
        if not side.content_uri:
            raise AhpClientError(
                f"{side.uri or 'this file side'} carries no ContentRef; there is nothing to read"
            )
        return (await self.read_content(side.content, encoding=encoding)).decode(encoding)

    # ── review ───────────────────────────────────────────────────────────────

    def mark_reviewed(self, file_ids: Sequence[str] | str, *, reviewed: bool = True) -> None:
        """Tick (or clear) the GitHub-style "Viewed" box on one or more files.

        The one client-dispatchable action on this channel, and it is
        capability-gated: "Requires the changeset to advertise
        `capabilities.review`", read from the *current* catalogue entry because
        that is what the host validates against. Refusing here costs nothing;
        sending it anyway earns a rejected echo that reverts the optimistic tick
        a moment after the box appeared to move.

        **Synchronous, like ``dispatch`` itself** (invariant 6): the action is
        numbered and enqueued before this returns, and an ``async def`` would
        imply the host had answered when write-ahead means it has not. Watch
        :meth:`changes` for the echo.

        Ids that are not in :meth:`files` are refused, when there is a file list
        to check against. "Ids that do not match a file currently present in the
        changeset are ignored; if none match, the action is a no-op" -- the same
        silent no-op the empty list is refused for, except that this one is
        *knowable*: the parameter is literally :attr:`ChangesetFile.id` and the
        list is in hand. (The rename argument that keeps ``invoke(resource=…)``
        unchecked does not reach here; a target may legitimately name
        ``before.uri``, a review id may not.)
        """
        ids = [file_ids] if isinstance(file_ids, str) else list(file_ids)
        present = {f.id for f in self.files()}
        if present:
            unknown = sorted(set(ids) - present)
            if unknown:
                raise InvalidArgument(
                    f"changeset {self.uri} has no files {unknown}; the reducer would ignore "
                    f"them silently. It has {sorted(present)}"
                )
        entry = self.info
        if entry is None:
            raise AhpClientError(
                f"changeset {self.uri} has no catalogue entry on session {self._session.uri}, so "
                "nothing advertises capabilities.review; open it from Session.changesets()"
            )
        if not entry.reviewable:
            raise AhpClientError(
                f"changeset {entry.label or self.uri!r} does not advertise capabilities.review; "
                "clients that omit handling MUST treat it as non-reviewable"
            )
        self._client.protocol.dispatch(self.uri, actions.files_review_changed(ids, reviewed))

    # ── operations ───────────────────────────────────────────────────────────

    async def invoke(
        self,
        operation_id: str,
        *,
        resource: str | None = None,
        range: Mapping[str, Any] | None = None,  # noqa: A002 - `TextRange`'s own name
        side: Literal["before", "after"] | None = None,
        confirmed: bool = False,
    ) -> JsonObject:
        """Run a server-declared operation, after checking what it declared.

        The target is inferred from what you pass -- a *range* implies the
        ``range`` scope, a bare *resource* implies ``resource``, neither implies
        ``changeset`` -- and then checked against **this changeset's** own
        ``operations``, because "the server validates that `operationId` exists
        in the changeset's current `operations` list and that the requested
        `target.kind` is contained in the operation's `scopes`. Invalid
        combinations result in a JSON-RPC error."

        *side* (``before``/``after``) is part of the target and is only
        meaningful with one. It is a **closed enum on both target branches**,
        checked here because nothing else in the stack does: the sibling host
        validates ``kind``, ``resource`` and both range positions and never
        reads ``side`` at all, so an arbitrary string reaches a handler that
        branches on it and reads it as neither side. This is the same defect
        ``_text_range`` refuses to leave open, on the field beside it.

        *confirmed* is the caller asserting the user accepted
        :attr:`ChangesetOperation.confirmation_text`. It is required whenever
        the operation carries one, because that is a client MUST and a library
        that sent it anyway would make every caller quietly violate it.

        Returns ``InvokeChangesetOperationResult``: "success is implicit", and
        an optional ``message`` / ``followUp``. What the operation *did* arrives
        separately, as ``changeset/*`` actions -- and a later failure arrives as
        :attr:`ChangesetOperation.error`, not as an exception here.

        **No optimistic state is written.** "Clients SHOULD NOT synthesise local
        optimistic changes for invocations unless the server explicitly opts in
        via a future capability."
        """
        operation = self.operation(operation_id)
        if operation is None:
            declared = sorted(o.id for o in self.operations())
            raise AhpClientError(
                f"changeset {self.uri} declares no operation {operation_id!r}"
                + (f"; it declares {declared}" if declared else " and declares none at all")
            )
        # Runtime, not only the annotation: `mypy --strict` does not run in the
        # caller's process, and every other guard in this method is a real check
        # for the same reason.
        if side is not None and side not in _SIDES:
            raise InvalidArgument(
                f"target.side is {sorted(_SIDES)} or absent, not {side!r}; the host does not "
                "validate it, so an unknown value reaches a handler as neither side"
            )
        kind = "range" if range is not None else "resource" if resource is not None else "changeset"
        if kind not in operation.scopes:
            raise AhpClientError(
                f"operation {operation_id!r} declares scopes {sorted(operation.scopes)}, "
                f"not {kind!r}"
            )
        if kind != "changeset" and not resource:
            raise AhpClientError(f"a {kind} target needs the resource it acts on")
        if kind == "changeset" and side is not None:
            raise AhpClientError("side belongs to a resource or range target, not to a changeset")
        if operation.destructive and not confirmed:
            raise AhpClientError(
                f"operation {operation_id!r} carries a confirmation, which the client MUST show "
                f"and the user MUST accept before it runs: {operation.confirmation_text!r}. "
                "Pass confirmed=True once they have."
            )

        target: JsonObject | None = None
        if kind != "changeset":
            target = {"kind": kind, "resource": resource}
            if side is not None:
                target["side"] = side
            if range is not None:
                target["range"] = _text_range(range)
        return await self._client.protocol.invoke_changeset_operation(
            self.uri, operation_id=operation_id, target=target
        )

    # ── watching ─────────────────────────────────────────────────────────────

    async def changes(self) -> AsyncIterator[Changeset]:
        """Yield ``self`` whenever anything that moves this changeset lands.

        The reducer is the source of truth and the envelope is only the clock,
        which is the same shape :meth:`Session.inputs` uses and for the same
        reason: ``files``, ``operations`` and ``status`` move for eight
        different reasons and re-deriving them from the actions means
        re-implementing the reducer that already ran.

        A **rejected** envelope is a clock too. It is how an optimistic
        :meth:`mark_reviewed` is reverted, and a caller rendering a tick box
        needs to see the box move back.

        **``session/changesetsChanged`` is also a clock**, even though it lands
        on the *session* channel. :attr:`info` is deliberately live, and the
        scenario it is live for -- a changeset that stops being reviewable
        mid-session, which the sibling host has a test for -- moves nothing on
        this channel at all. Waking only here meant the one loop this API offers
        could not observe the one thing the re-read was built for: the tick
        boxes stayed on screen and clicking one raised locally.

        Ends when :meth:`aclose` is called. A loop that outlives its
        subscription reports on a channel this client no longer receives.
        """
        reader = self._client._runtime.events()
        self._readers.add(reader)
        try:
            async for tagged in reader:
                if not isinstance(tagged.event, ActionEvent):
                    continue
                on_channel = tagged.channel == self.uri
                on_catalogue = tagged.channel == self._session.uri and _is_catalogue_change(
                    tagged.event.envelope
                )
                if on_channel or on_catalogue:
                    yield self
                if self._closed:
                    return
        finally:
            self._readers.discard(reader)
            await reader.aclose()

    async def wait_until_ready(self, timeout: float = 30.0) -> Changeset:
        """Block until ``status`` settles on ``ready`` or ``error``.

        A caller that subscribes right after a turn otherwise reads an empty
        ``files`` list and concludes the agent changed nothing -- the host
        registers the channel in ``computing`` and returns to it on every
        refresh, precisely so a client can show progress instead of an empty
        diff.

        **A channel with no state at all is not "settled".** Testing
        ``status != "computing"`` returned instantly for the empty string, which
        is what an unsubscribed, disposed or never-registered channel reads as --
        blessing exactly the empty file list this method exists to prevent a
        caller from believing. So the wait is for a status the protocol defines,
        and a dead channel raises instead.
        """
        if not self.live:
            raise AhpClientError(
                f"changeset {self.uri} is not subscribed, so it will never become ready; "
                "it was closed, or the host registered no such channel"
            )
        reader = self._client._runtime.events()
        self._readers.add(reader)
        try:
            if self.status in _SETTLED:
                return self
            async with asyncio.timeout(timeout):
                async for tagged in reader:
                    if tagged.channel == self.uri and self.status in _SETTLED:
                        return self
                    if self._closed:
                        break
        except TimeoutError as exc:
            raise RequestTimeout(
                f"changeset {self.uri} to settle (it is still {self.status or 'unpublished'!r})",
                timeout,
            ) from exc
        finally:
            self._readers.discard(reader)
            await reader.aclose()
        raise AhpClientError(f"changeset {self.uri} was closed before it became ready")

    async def aclose(self) -> None:
        """Release this handle's subscription and end its watchers.

        A changeset channel is the host's -- there is no ``disposeChangeset``
        and the host drops it with the session -- so this is a *local* release
        and it is refcounted in the runtime. Two handles on one changeset is the
        ordinary case (`open_changeset` mints a fresh object per call), and an
        unrefcounted unsubscribe blinded the other one: empty files, instant
        waits, dispatches into a channel nobody was receiving, no error.
        """
        self._closed = True
        for reader in list(self._readers):
            await reader.aclose()
        self._readers.clear()
        await self._client._runtime.unsubscribe(self.uri)


def _text_range(value: Mapping[str, Any]) -> JsonObject:
    """Check a ``TextRange`` before it goes out.

    A range whose ``end`` is missing a ``character`` is accepted by the wire
    schema's loosest reading and then means whatever the host decides -- which
    for a `revert this range` operation is not a thing to leave open.

    ``line`` and ``character`` are declared ``"type": "number"``, so an integral
    float is conformant and is accepted -- refusing one is a false refusal
    against a host that would take it, which this module declines to do
    elsewhere on principle. A *fractional* offset is not a position.
    """
    checked: JsonObject = {}
    for edge in ("start", "end"):
        position = value.get(edge)
        if not isinstance(position, Mapping):
            raise InvalidArgument(f"a TextRange needs a {edge} position; use text_range()")
        axes: JsonObject = {}
        for axis in ("line", "character"):
            raw = position.get(axis)
            if not isinstance(raw, int | float) or isinstance(raw, bool) or raw != int(raw):
                raise InvalidArgument(f"TextRange.{edge}.{axis} must be a zero-based integer")
            axes[axis] = int(raw)
        checked[edge] = axes
    return checked


def _decode(result: Mapping[str, Any]) -> bytes:
    """``ResourceReadResult`` -> bytes.

    "Binary content MUST use `base64`; text content MAY use `utf-8`", so the
    encoding is the receiver's choice and a caller who assumes text gets a
    base64 blob the first time a changeset touches a PNG. Not shared with
    ``serve/resources.py``'s decoder, which raises the error a *host* answers
    with; this is a client reading a reply.
    """
    data = _string(result.get("data"))
    if result.get("encoding") != "base64":
        return data.encode("utf-8")
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AhpClientError("the host answered resourceRead with invalid base64") from exc


def _is_catalogue_change(envelope: Mapping[str, Any]) -> bool:
    action = envelope.get("action")
    return isinstance(action, Mapping) and action.get("type") == "session/changesetsChanged"


def changeset_catalogue(state: Mapping[str, Any]) -> Iterator[ChangesetInfo]:
    """``SessionState.changesets`` as entries. Absent means none."""
    raw = state.get("changesets")
    if not isinstance(raw, list):
        return
    for entry in raw:
        if isinstance(entry, dict):
            yield ChangesetInfo(entry)
