"""Resource watches: telling a client when something on disk changed.

Three things about this channel are easy to get wrong, and all three are
security or correctness properties rather than features.

**The watch id must be unguessable.** `CreateResourceWatchResult.channel` is
specified as *receiver-assigned*, so minting an opaque random id is the
conformant behaviour -- VS Code's base64-of-the-descriptor scheme is an
implementation choice, not something a host must mirror. It matters because a
watch channel is subscribable by anyone who can name it, and a derivable name is
a name every peer already has.

**A watcher must not outlive its audience.** The channel is registered when the
watch is created and torn down when its last subscriber goes, which is what the
sequencer's subscription lifecycle hooks exist for. Without that, a client that
walks away leaves a poller running over a directory tree for the life of the
host.

**Changes must be coalesced.** A build or a `git checkout` produces thousands of
events in a second. Each one published individually is one `serverSeq`, one
reducer pass and one fan-out per subscriber -- and, because the replay log is
bounded, enough of them to evict this channel's own history. They are batched
into one action per interval instead.

The default watcher polls. That is not free, and it is the reason
:class:`PollingResourceWatcher` takes an entry cap and says out loud when it
hits one: a watch that silently stopped covering half a tree is worse than one
that refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import os
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from agent_host_server.core.resources import path_from_file_uri, uri_from_path

__all__ = [
    "PollingResourceWatcher",
    "ResourceChange",
    "ResourceWatcher",
    "WatchRequest",
    "new_watch_channel",
]

_log = logging.getLogger(__name__)

#: Enough that a `git checkout` lands as a handful of actions rather than
#: thousands, and short enough that an editor's save feels immediate.
DEFAULT_COALESCE_SECONDS = 0.2

#: Entries a polling watch will track before it gives up and says so. A
#: `node_modules` is a million files; walking it twice a second is not a watch,
#: it is a fan.
DEFAULT_MAX_ENTRIES = 20_000


@dataclass(frozen=True)
class ResourceChange:
    uri: str
    type: str  # 'created' | 'changed' | 'deleted'

    def to_wire(self) -> dict[str, str]:
        return {"uri": self.uri, "type": self.type}


@dataclass(frozen=True)
class WatchRequest:
    """What a client asked to watch. Also this channel's entire state."""

    root: str
    recursive: bool = False
    excludes: Sequence[str] = ()
    includes: Sequence[str] = ()

    def to_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"root": self.root, "recursive": self.recursive}
        if self.excludes:
            state["excludes"] = {"items": list(self.excludes)}
        if self.includes:
            state["includes"] = {"items": list(self.includes)}
        return state

    def matches(self, relative: str) -> bool:
        """Whether a path relative to the root should be reported.

        `includes` is a whitelist when present; `excludes` always wins. Both are
        matched with `fnmatch` against the relative path, and against each
        leading directory, so `node_modules` excludes everything beneath it
        without the caller writing `node_modules/**`.
        """
        parts = PurePosixPath(relative).parts
        prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        for pattern in self.excludes:
            if any(fnmatch.fnmatch(p, pattern) for p in prefixes):
                return False
        if not self.includes:
            return True
        return any(fnmatch.fnmatch(p, pattern) for p in prefixes for pattern in self.includes)


@runtime_checkable
class ResourceWatcher(Protocol):
    """Watches one URI and calls back with batches of changes.

    Supplied by the embedder when polling is not good enough -- an inotify or
    FSEvents binding, or a watcher over something that is not a filesystem at
    all.
    """

    async def start(
        self, request: WatchRequest, emit: Callable[[Sequence[ResourceChange]], Any]
    ) -> None: ...

    async def stop(self, request: WatchRequest) -> None: ...


def new_watch_channel() -> str:
    """An unguessable watch channel URI.

    "Receiver-assigned" is the spec's word, and an unguessable id is what makes
    that meaningful: a watch channel is subscribable by anyone who can name it.
    """
    return f"ahp-resource-watch:/{secrets.token_urlsafe(16)}"


@dataclass
class _Poll:
    seen: dict[str, tuple[int, int]]
    task: asyncio.Task[None] | None = None


class PollingResourceWatcher:
    """Detects change by re-walking the tree. Dependency-free, and honest about it.

    Polling is the wrong tool for a large tree and the only portable one that
    needs no third-party package. The entry cap is what keeps it from becoming a
    background CPU load nobody asked for, and hitting it is logged rather than
    silently tolerated -- a watch that quietly stopped covering half a directory
    is worse than one that refused.
    """

    def __init__(
        self,
        *,
        interval: float = 1.0,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.interval = interval
        self.max_entries = max_entries
        self._polls: dict[str, _Poll] = {}

    async def start(
        self, request: WatchRequest, emit: Callable[[Sequence[ResourceChange]], Any]
    ) -> None:
        if request.root in self._polls:
            return
        # The baseline is taken BEFORE the loop starts, so the first tick
        # reports what changed since the watch was created rather than
        # announcing every file in the tree as newly created.
        poll = _Poll(seen=self._scan(request))
        poll.task = asyncio.create_task(self._loop(request, emit, poll))
        self._polls[request.root] = poll

    async def stop(self, request: WatchRequest) -> None:
        poll = self._polls.pop(request.root, None)
        if poll is None:
            return
        if poll.task is None:
            return
        poll.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll.task

    async def _loop(
        self,
        request: WatchRequest,
        emit: Callable[[Sequence[ResourceChange]], Any],
        poll: _Poll,
    ) -> None:
        while True:
            await asyncio.sleep(self.interval)
            current = self._scan(request)
            changes = list(self._diff(poll.seen, current))
            poll.seen = current
            if changes:
                result = emit(changes)
                if asyncio.iscoroutine(result):
                    await result

    @staticmethod
    def _diff(
        before: Mapping[str, tuple[int, int]], after: Mapping[str, tuple[int, int]]
    ) -> Iterable[ResourceChange]:
        for uri, stamp in after.items():
            if uri not in before:
                yield ResourceChange(uri, "created")
            elif before[uri] != stamp:
                yield ResourceChange(uri, "changed")
        for uri in before:
            if uri not in after:
                yield ResourceChange(uri, "deleted")

    def _scan(self, request: WatchRequest) -> dict[str, tuple[int, int]]:
        try:
            root = path_from_file_uri(request.root)
        except Exception:
            # A watch on something this watcher cannot walk -- a `virtual:` URI,
            # say. It reports nothing rather than failing the subscription; an
            # embedder with such URIs supplies its own watcher.
            return {}

        found: dict[str, tuple[int, int]] = {}
        truncated = False
        for directory, subdirectories, files in os.walk(root):
            relative_dir = os.path.relpath(directory, root)
            if not request.recursive and relative_dir != ".":
                subdirectories.clear()
                continue
            # Pruned in place so an excluded directory is never descended into,
            # which is the difference between skipping `node_modules` and
            # walking it and discarding the results.
            subdirectories[:] = [
                name for name in subdirectories if request.matches(_join(relative_dir, name))
            ]
            for name in files:
                relative = _join(relative_dir, name)
                if not request.matches(relative):
                    continue
                if len(found) >= self.max_entries:
                    truncated = True
                    break
                try:
                    stats = os.lstat(os.path.join(directory, name))
                except OSError:
                    continue
                found[uri_from_path(Path(directory) / name)] = (stats.st_mtime_ns, stats.st_size)
            if truncated:
                break
        if truncated:
            _log.warning(
                "resource watch on %s hit the %d-entry cap; changes beyond it are NOT reported",
                request.root,
                self.max_entries,
            )
        return found


def _join(relative_dir: str, name: str) -> str:
    return name if relative_dir == "." else f"{relative_dir}/{name}"
