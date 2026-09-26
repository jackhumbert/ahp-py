"""Sessions that outlive the process.

A host restart currently loses every session. This is the layer that stops it,
and it is the one place in the library where data a peer controls is written to
disk and then read back as *structure*. Two decisions carry that weight.

**JSON, and nothing else.** Never `pickle`, never `marshal`, never `shelve`,
never any format that honours `__reduce__` -- not as a default, not as an
option, not behind a flag. A stored session is not the host's own data: the
title, the whole chat transcript, every tool result and the provider's resume
blob all arrived over the wire from a peer. Loading a serialised object graph of
that *is* executing it, and `docs/roadmap.md` §10 names this "the single most
likely Python-shaped remote-code-execution mistake in the whole roadmap" --
permanently out of scope, not deferred. `json.loads` constructs dicts, lists,
strings, numbers, booleans and `None`, and calls nothing.

**The filename is a hash; the URI never touches the path.** A session URI is
client-chosen and opaque (invariant 15). VS Code happens to send
`<provider>:/<uuid>`, but nothing in the protocol stops a peer from creating one
that is `../../../../etc/cron.d/x`, or contains a NUL, or is four kilobytes
long. Used as a path component that is an arbitrary-file-write primitive, so it
is not used as one: the file is named by the SHA-256 of the URI, and the real
URI is recorded *inside* the file, where it is data.

Three ordinary durability properties on top of that:

* **Writes are atomic.** A sibling temp file, `fsync`, then `os.replace`. The
  failure being avoided is not a lost write but a plausible one -- a truncated
  JSON file can still parse, and then the session comes back with a transcript
  that stops mid-sentence and nothing anywhere reports a fault.
* **An unreadable file is skipped, not fatal.** One corrupt session must not
  stop a host from starting with the other nine; refusing to start loses all ten
  to save one.
* **Saves are debounced.** A streaming turn changes channel state every few
  tokens. A store that fsynced each one would put a disk round-trip in the
  middle of the fan-out path, so `save_soon` coalesces and the most recent state
  wins.

Owner-only modes throughout -- 0o600 on files, 0o700 on the directory -- for the
reason `core/wirelog.py` does the same: what is on disk is the entire
conversation in plaintext.

There is no default persistence, matching `core/seq.py`. `InMemorySessionStore`
is what a host gets unless the embedder names a location; this library does not
choose filesystem paths on anyone's behalf.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "DEFAULT_DEBOUNCE_SECONDS",
    "FileSessionStore",
    "InMemorySessionStore",
    "SessionStore",
    "StoredSession",
]

_log = logging.getLogger(__name__)

#: Long enough that a turn's worth of state changes lands as one write, short
#: enough that a host killed a moment after a turn keeps that turn.
DEFAULT_DEBOUNCE_SECONDS = 1.0

#: Bumped only when the payload shape changes incompatibly. A file written by a
#: future version is skipped whole rather than half-read into the wrong fields.
_FORMAT_VERSION = 1


@dataclass(frozen=True)
class StoredSession:
    """One session, flattened to values `json` can represent.

    `channels` is keyed by channel URI rather than by role because the URIs are
    client-chosen and are not derivable from one another (invariant 15) -- the
    session channel, its chat, and its annotations each go in under the name the
    host registered them with, and come back bound to the same reducer.

    `resume_state` is the provider's, and opaque here: the host round-trips it
    and never looks inside (ADR 0003). `metadata` gets the same treatment for
    the EMBEDDER.
    """

    uri: str
    provider: str
    created_at: str
    channels: Mapping[str, Mapping[str, Any]]
    title: str | None = None
    resume_state: Mapping[str, Any] | None = None
    #: The embedder's, round-tripped verbatim and never interpreted.
    #:
    #: This exists so durability and partitioning can both be on. Without it a
    #: `StoredSession` carried channels, title, provider and resume state --
    #: everything except WHO IT BELONGS TO -- so a restored session had no
    #: owner, `may_see_channel` refused unowned channels, and restoring one
    #: produced a session nobody could reach. `may_restore_session` refusing by
    #: default was a correct answer to a missing capability, not a design
    #: position.
    #:
    #: An embedder could keep ownership in a second store and re-claim before
    #: serving, which is two records of the same fact with nothing keeping them
    #: in step. Ownership belongs with the session it describes.
    metadata: Mapping[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": _FORMAT_VERSION,
            "uri": self.uri,
            "provider": self.provider,
            "createdAt": self.created_at,
            "channels": {uri: dict(state) for uri, state in self.channels.items()},
        }
        # Absent rather than null, so a session that never had a title cannot be
        # read back as one whose title was explicitly cleared (invariant 18).
        if self.title is not None:
            payload["title"] = self.title
        if self.resume_state is not None:
            payload["resumeState"] = dict(self.resume_state)
        if self.metadata is not None:
            payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_json(cls, payload: Any) -> StoredSession:
        """Rebuild from parsed JSON, or raise `ValueError`.

        Every field is checked rather than trusted. What is on disk was peer
        content and may have been truncated, hand-edited or written by another
        version; a `channels` that is a list would otherwise surface as an
        `AttributeError` during startup, far from the file that caused it.
        """
        if not isinstance(payload, Mapping):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
        version = payload.get("version")
        if version != _FORMAT_VERSION:
            raise ValueError(f"unsupported store format version: {version!r}")

        raw_channels = payload.get("channels")
        if not isinstance(raw_channels, Mapping):
            raise ValueError("channels must be an object")
        channels: dict[str, Mapping[str, Any]] = {}
        for uri, state in raw_channels.items():
            if not isinstance(uri, str) or not isinstance(state, Mapping):
                raise ValueError("channels must map a channel URI to an object")
            channels[uri] = dict(state)

        title = payload.get("title")
        if title is not None and not isinstance(title, str):
            raise ValueError("title must be a string or absent")
        resume_state = payload.get("resumeState")
        if resume_state is not None and not isinstance(resume_state, Mapping):
            raise ValueError("resumeState must be an object or absent")
        metadata = payload.get("metadata")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValueError("metadata must be an object or absent")

        return cls(
            uri=_required_string(payload, "uri"),
            provider=_required_string(payload, "provider"),
            created_at=_required_string(payload, "createdAt"),
            channels=channels,
            title=title,
            resume_state=dict(resume_state) if resume_state is not None else None,
            metadata=dict(metadata) if metadata is not None else None,
        )


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {type(value).__name__}")
    return value


class SessionStore(Protocol):
    """Where a host's sessions live between processes.

    Deliberately small. The host owns the data model; a store that understood
    channels would need revising on every spec bump, and every implementor would
    have to follow.
    """

    async def save(self, session: StoredSession) -> None:
        """Persist `session`, returning only once it is durable."""
        ...

    async def save_soon(self, session: StoredSession) -> None:
        """Persist `session` shortly, coalescing with other recent saves for the
        same URI. The most recent value wins; nothing in between is written."""
        ...

    async def load_all(self) -> Sequence[StoredSession]:
        """Every session that could be read. Unreadable ones are skipped."""
        ...

    async def delete(self, uri: str) -> None:
        """Forget `uri`, including anything `save_soon` still owes for it."""
        ...

    async def flush(self) -> None:
        """Await every write `save_soon` has outstanding."""
        ...

    async def aclose(self) -> None:
        """Flush and release. Final: `save_soon` afterwards is an error."""
        ...


class InMemorySessionStore:
    """Keeps nothing, which is why a host is ephemeral unless asked otherwise.

    Not a dictionary of sessions. The host already holds its live sessions, and
    a store that kept a parallel copy of every transcript would double the
    memory cost of the thing most likely to be large, to serve a `load_all` that
    on a fresh process can only ever be empty. So `save` is a no-op and
    `load_all` is empty -- honest about the fact that nothing here survives the
    process.
    """

    async def save(self, session: StoredSession) -> None:
        return None

    async def save_soon(self, session: StoredSession) -> None:
        return None

    async def load_all(self) -> Sequence[StoredSession]:
        return ()

    async def delete(self, uri: str) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class FileSessionStore:
    """One JSON file per session, in one directory the embedder names.

    A file per session rather than a single document because the write pattern
    is per-session: a turn in one chat should not rewrite -- and cannot corrupt
    -- the other nine sessions' transcripts.
    """

    def __init__(self, directory: Path, *, debounce: float = DEFAULT_DEBOUNCE_SECONDS) -> None:
        if debounce < 0:
            raise ValueError("debounce must not be negative")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # `mkdir`'s mode is masked by the umask and ignored entirely when the
        # directory already exists, so owner-only is asserted rather than
        # requested. Suppressed where there are no mode bits (Windows).
        with contextlib.suppress(OSError):
            os.chmod(self.directory, 0o700)
        self._debounce = debounce
        self._pending: dict[str, StoredSession] = {}
        self._timer: asyncio.Task[None] | None = None
        # One writer at a time, and the reason `flush` means what it says: a
        # drain cannot begin while a write is in flight, so a `flush` that
        # returns has genuinely waited for the disk.
        self._writing = asyncio.Lock()
        self._closed = False

    def _path_for(self, uri: str) -> Path:
        """The file backing `uri`. Never `uri` itself.

        The URI is client-chosen and opaque, so it may contain `/` or `..`, may
        contain a NUL, may be longer than any filesystem's name limit, and --
        having come out of a JSON parser -- may contain a lone surrogate that
        plain UTF-8 encoding refuses. Hashing answers all of those at once: the
        result is 64 hex characters whatever went in, and cannot name a
        directory entry outside `self.directory`.
        """
        digest = hashlib.sha256(uri.encode("utf-8", "surrogatepass")).hexdigest()
        return self.directory / f"{digest}.json"

    # ─── writing ─────────────────────────────────────────────────────────

    async def save(self, session: StoredSession) -> None:
        async with self._writing:
            # A queued debounce for this URI is superseded by an explicit save,
            # not written on top of it a second later.
            self._pending.pop(session.uri, None)
            await self._write(session)

    async def save_soon(self, session: StoredSession) -> None:
        if self._closed:
            raise RuntimeError("session store is closed")
        # Replacing rather than queueing is what makes the last state win. A
        # queue would write every intermediate transcript, in order, and arrive
        # at the same file having done the work n times.
        self._pending[session.uri] = session
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._after_debounce())

    async def flush(self) -> None:
        await self._drain()

    async def aclose(self) -> None:
        self._closed = True
        await self.flush()
        timer, self._timer = self._timer, None
        if timer is not None and not timer.done():
            # Only a sleep can be interrupted here: the flush above emptied the
            # queue under the lock, so a timer that does reach `_drain` finds
            # nothing to write. (A `to_thread` write could not be interrupted
            # anyway -- the thread finishes regardless -- which is also why
            # cancelling this store can never leave a torn file.)
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer

    async def _after_debounce(self) -> None:
        await asyncio.sleep(self._debounce)
        await self._drain()

    async def _drain(self) -> None:
        async with self._writing:
            while self._pending:
                # Taken whole and replaced in one step with no `await` between:
                # anything arriving during the writes below lands in the NEW
                # dict and is picked up by the next pass, so the most recent
                # state is never the one left behind.
                batch, self._pending = self._pending, {}
                for session in batch.values():
                    try:
                        await self._write(session)
                    except (OSError, TypeError, ValueError) as exc:
                        # Nobody is awaiting a debounced save, so there is
                        # nowhere to raise. Dropping it silently would make a
                        # store that has quietly stopped persisting look exactly
                        # like one that is working.
                        _log.warning("could not persist session %s: %s", session.uri, exc)

    async def _write(self, session: StoredSession) -> None:
        # `ensure_ascii` is not cosmetic here: a transcript can carry a lone
        # surrogate, which JSON permits and UTF-8 encoding does not, and the
        # escaped form is the only one that survives the round trip. NaN and
        # Infinity are left as Python emits them -- non-standard JSON, but
        # refusing them would lose a whole session over one number a peer sent.
        payload = json.dumps(session.to_json(), ensure_ascii=True)
        # Off the event loop thread. An `fsync` blocks for milliseconds at best,
        # and this loop is also every connection's single writer (invariant 10).
        await asyncio.to_thread(_write_atomically, self._path_for(session.uri), payload)

    # ─── reading and forgetting ──────────────────────────────────────────

    async def load_all(self) -> Sequence[StoredSession]:
        return await asyncio.to_thread(self._load_all)

    def _load_all(self) -> list[StoredSession]:
        try:
            paths = sorted(self.directory.glob("*.json"))
        except OSError as exc:
            _log.warning("session store %s is unreadable: %s", self.directory, exc)
            return []
        sessions: list[StoredSession] = []
        for path in paths:
            try:
                session = StoredSession.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                # A torn tail, a hand-edited file, a snapshot from a version
                # that wrote a different shape. Refusing to start would lose
                # every *other* session to save this one.
                _log.warning("skipping unreadable session file %s: %s", path, exc)
                continue
            sessions.append(session)
        return sessions

    async def delete(self, uri: str) -> None:
        async with self._writing:
            # Under the lock, and before the unlink: a debounce still holding
            # this session would otherwise write the file back moments after it
            # was disposed, resurrecting it on the next start.
            self._pending.pop(uri, None)
            await asyncio.to_thread(self._path_for(uri).unlink, missing_ok=True)


def _write_atomically(path: Path, text: str) -> None:
    """Write `text` to `path` so no reader can ever observe a partial file.

    Sibling, `fsync`, `os.replace` -- `replace` is atomic on POSIX and on
    Windows, so a crash leaves either the whole old file or the whole new one.
    Writing in place would add a third outcome: a prefix of the new content,
    which for JSON can still parse into a shorter transcript that looks
    entirely well-formed.
    """
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        # `O_EXCL` so this can never open something already at that name --
        # including a symlink pointing somewhere else -- and 0o600 at creation
        # rather than a `chmod` afterwards, so the transcript is never even
        # briefly readable by anyone else.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        # Otherwise a full disk or a serialisation fault leaves a temp file per
        # attempt, forever, next to the data it failed to become.
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    # `replace` is atomic but its directory entry is not durable until the
    # directory itself is synced. Without this a crash can lose the rename and
    # leave the previous file in place -- safe, but silently stale.
    with contextlib.suppress(OSError):
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
