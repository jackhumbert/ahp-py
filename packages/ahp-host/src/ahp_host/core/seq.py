"""Where ``serverSeq`` comes from.

``serverSeq`` is host-global and monotonic. In memory that is a counter; across
a restart it is a problem, because **a client remembers the number**.

The reference TypeScript client takes a *maximum* when it records what it has
seen (`clients/typescript/src/client/hosts/runtime.ts:679-681`). So a host that
restarts and begins again at 0 leaves that client holding a number the host will
not reach again for a long time. Its `lastSeenServerSeq` stays permanently ahead,
and it can never replay -- not until the next reconnect, but ever. The
sequencer's epoch check turns that into "take fresh snapshots", which is correct
but costs a full state transfer on every single reconnect for the rest of the
host's life.

A monotonic counter that survives the restart closes it. Numbers are reserved in
**blocks** so the common path is a bare increment with no I/O: a block is
persisted once, and a crash inside it skips the rest of the block rather than
replaying numbers. Skipping is safe -- nothing in the protocol requires
``serverSeq`` to be contiguous, only increasing -- while *reusing* a number would
let a stale client mistake new state for state it already has.

There is no default persistence. `InMemorySequence` is what a host gets unless
the embedder chooses a location, matching this library's position that it does
not pick filesystem paths on anyone's behalf.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

__all__ = ["FileSequence", "InMemorySequence", "SequenceAllocator"]

#: Numbers reserved per persisted write. Large enough that the fsync is rare,
#: small enough that a crash does not skip a visible chunk of the number space.
_BLOCK = 1000


class SequenceAllocator(Protocol):
    """Hands out ``serverSeq`` values. Called only inside the sequencer's lock."""

    def next(self) -> int:
        """The next sequence number. MUST be strictly greater than the last."""
        ...

    def current(self) -> int:
        """The high-water mark: no number at or below this will be issued again.

        Read once at construction so the sequencer starts where the previous
        process stopped rather than at zero. It need not be a number that was
        actually issued -- a block-reserving allocator returns its ceiling,
        which is conservative in the safe direction.
        """
        ...


class InMemorySequence:
    """Starts at 1 every time the process does.

    Correct for a host whose sessions do not outlive it -- there is no state for
    a client to be stale about -- and wrong for one whose do.
    """

    def __init__(self, start: int = 0) -> None:
        self._value = start

    def next(self) -> int:
        self._value += 1
        return self._value

    def current(self) -> int:
        return self._value


class FileSequence:
    """A monotonic counter persisted to one small file.

    The file holds the **ceiling** of the reserved block, not the last number
    issued: on restart the ceiling is where allocation resumes, so a number
    issued before a crash can never be issued again.

    Writes are atomic (write a sibling, ``fsync``, ``os.replace``) because a
    torn counter file is worse than a missing one -- a short read could parse as
    a smaller number and start handing out sequence numbers a client has
    already seen.
    """

    def __init__(self, path: Path, *, block: int = _BLOCK) -> None:
        if block < 1:
            raise ValueError("block must be at least 1")
        self.path = Path(path)
        self._block = block
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._value = self._read()
        self._ceiling = self._value
        self._reserve()

    def _read(self) -> int:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return 0
        try:
            return max(0, int(text))
        except ValueError:
            # An unreadable counter is not a reason to restart at zero, which is
            # the one outcome this class exists to prevent. Refuse instead.
            raise ValueError(f"{self.path} is not a sequence counter: {text!r}") from None

    def _reserve(self) -> None:
        self._ceiling = self._value + self._block
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(f"{self._ceiling}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def next(self) -> int:
        self._value += 1
        if self._value > self._ceiling:
            self._reserve()
        return self._value

    def current(self) -> int:
        # The previous ceiling, not the last number issued -- the remainder of
        # a reserved block is skipped on restart, so this is at or above every
        # number ever handed out. Conservative in the safe direction: a client
        # is never told a number is fresh when it has already seen it.
        return self._value
