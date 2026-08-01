"""The `resource*` family: reading things the host can see.

This is the largest hole a full AHP host opens. Implementing the family
literally hands any peer that completes `initialize` a filesystem API, and the
protocol offers nothing to close it: there is no server capability object, and
`may_see_channel` -- the only per-resource hook the host otherwise has -- buys
nothing here, because **every** `resource*` command targets `ahp-root://`. So
the gate is a dedicated one, and it is mandatory.

Three layers, in this order:

1. **A null default.** :class:`NullResourceProvider` is what a host gets unless
   the embedder installs something else, and it answers `NotFound` to
   everything. A host does not acquire a filesystem by upgrading.
2. **The jail.** :class:`RootedFilesystemResourceProvider` walks a path one
   component at a time with `openat`, refusing to follow any symlink it has not
   itself resolved *and re-checked against the root*. This is not
   `realpath`-then-open: that has a window in which a component can be swapped
   for a symlink after the check and before the open, which is the entire
   classic filesystem-jail escape.
3. **The policy.** :meth:`Policy.may_access_resource` sees the **canonical**
   URI, after resolution, so it is deciding about the file that will actually be
   read rather than the name the peer used.

The write half is here too, behind a **second, separate opt-in**
(`writable=True`). Read access discloses; write access destroys, and the two
should never be granted by the same gesture. Every mutating call also takes the
per-path lock, because `append` has to evaluate EOF and write as one step -- the
spec requires it to be atomic "with respect to other appenders", and two
concurrent appends that both read EOF first will overwrite each other.
"""

from __future__ import annotations

import asyncio
import errno as _errno
import mimetypes
import os
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import unquote, urlparse

from agent_host_server.core import errors

__all__ = [
    "DirectoryEntry",
    "NullResourceProvider",
    "ResourceAccess",
    "ResourceContent",
    "ResourceInfo",
    "ResourceProvider",
    "RootedFilesystemResourceProvider",
    "WritableResourceProvider",
    "path_from_file_uri",
    "uri_from_path",
]

#: How many symlinks may be resolved before a path is called a loop. POSIX
#: conventionally allows 40; a jail has no reason to be that generous.
_MAX_SYMLINKS = 16

ResourceAccess = Literal["resolve", "read", "list"]


@dataclass(frozen=True)
class ResourceInfo:
    """`ResourceResolveResult`. `uri` is the canonical one, post-resolution."""

    uri: str
    type: Literal["file", "directory", "symlink"]
    size: int | None = None
    mtime: str | None = None
    ctime: str | None = None
    content_type: str | None = None
    etag: str | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"uri": self.uri, "type": self.type}
        for key, value in (
            ("size", self.size),
            ("mtime", self.mtime),
            ("ctime", self.ctime),
            ("contentType", self.content_type),
            ("etag", self.etag),
        ):
            if value is not None:
                wire[key] = value
        return wire


@dataclass(frozen=True)
class ResourceContent:
    """Bytes, plus what they are. Encoding is chosen at the wire edge."""

    data: bytes
    content_type: str | None = None


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    type: Literal["file", "directory"]

    def to_wire(self) -> dict[str, str]:
        return {"name": self.name, "type": self.type}


@runtime_checkable
class ResourceProvider(Protocol):
    """Mediates access to whatever the host is willing to expose.

    Deliberately content-shaped rather than path-shaped: a provider may serve an
    in-memory blob store, a git object database, or a jailed directory, and only
    the last of those has paths at all.

    Every method raises :class:`~agent_host_server.core.errors.AhpError` with
    `NotFound` (-32008) or `PermissionDenied` (-32009); nothing here returns
    `None` for "no".
    """

    async def resolve(self, uri: str, *, follow_symlinks: bool = True) -> ResourceInfo: ...

    async def read(self, uri: str) -> ResourceContent: ...

    async def list_dir(self, uri: str) -> Sequence[DirectoryEntry]: ...


@runtime_checkable
class WritableResourceProvider(ResourceProvider, Protocol):
    """A provider that also mutates. Feature-detected, never assumed.

    Split from :class:`ResourceProvider` so a host cannot acquire write access
    by accident: the read commands work against any provider, and the write
    commands answer `PermissionDenied` unless the installed provider is
    *structurally* one of these.
    """

    async def write(
        self,
        uri: str,
        data: bytes,
        *,
        mode: str = "truncate",
        position: int = 0,
        create_only: bool = False,
        if_match: str | None = None,
    ) -> None: ...

    async def mkdir(self, uri: str) -> None: ...

    async def delete(self, uri: str, *, recursive: bool = False) -> None: ...

    async def move(
        self, source: str, destination: str, *, fail_if_exists: bool = False
    ) -> None: ...

    async def copy(
        self, source: str, destination: str, *, fail_if_exists: bool = False
    ) -> None: ...


class NullResourceProvider:
    """Exposes nothing. The default, and the reason a host is safe by omission.

    `NotFound` rather than `PermissionDenied`, deliberately: a host with no
    resource provider has no resources, and answering "denied" would tell a peer
    that something is there.
    """

    async def resolve(self, uri: str, *, follow_symlinks: bool = True) -> ResourceInfo:
        raise errors.AhpError(-32008, f"No such resource: {uri}")

    async def read(self, uri: str) -> ResourceContent:
        raise errors.AhpError(-32008, f"No such resource: {uri}")

    async def list_dir(self, uri: str) -> Sequence[DirectoryEntry]:
        raise errors.AhpError(-32008, f"No such resource: {uri}")


def path_from_file_uri(uri: str) -> Path:
    """A `file:` URI as a path, or `InvalidParams`.

    **`file:` only.** Every other scheme is refused rather than guessed at: a
    provider that mediates `git-blob:` or `virtual:` implements
    :class:`ResourceProvider` itself and never reaches here, and silently
    treating an unknown scheme as a path is how a jail acquires a second
    entrance.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise errors.invalid_params(f"not a file: URI: {uri}")
    if parsed.netloc not in ("", "localhost"):
        # A UNC-style authority names a different machine's filesystem, which
        # this provider does not mediate.
        raise errors.invalid_params(f"remote file URIs are not supported: {uri}")
    return Path(unquote(parsed.path))


def uri_from_path(path: Path) -> str:
    return path.as_uri()


def _iso(seconds: float) -> str:
    return (
        datetime.fromtimestamp(seconds, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class RootedFilesystemResourceProvider:
    """A read-only view of one directory, and nothing above it.

    The containment is enforced by *walking*, not by comparing strings. Each
    component is opened relative to the previous one with `O_NOFOLLOW`, so the
    kernel refuses to traverse a symlink the provider has not itself examined.
    A symlink that is found is resolved manually, and the result is re-checked
    against the root before the walk continues.

    That matters because the obvious implementation -- `realpath`, then compare
    prefixes, then open -- is wrong. Between the comparison and the open, any
    component can be replaced with a symlink pointing anywhere, and the open
    follows it. The check passed; the read escaped. Walking with `openat` closes
    the window because there is no window: the path is never re-interpreted from
    a string after it has been checked.
    """

    def __init__(self, root: Path, *, follow_symlinks: bool = True, writable: bool = False) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"resource root is not a directory: {self.root}")
        self._follow = follow_symlinks
        # A SECOND opt-in, on top of installing the provider at all. Reading
        # discloses; writing destroys, and the two should never be granted by
        # the same gesture (docs/roadmap.md section 6).
        self.writable = writable
        self._locks: dict[str, asyncio.Lock] = {}

    # ─── the jail ────────────────────────────────────────────────────────

    def _relative(self, uri: str) -> PurePosixPath:
        """The requested path, relative to the root, or `PermissionDenied`.

        `..` is rejected outright rather than normalised away: normalising is
        what makes `a/../../etc` look reasonable, and a legitimate client has no
        reason to send one.
        """
        path = path_from_file_uri(uri)
        if not path.is_absolute():
            raise errors.invalid_params(f"resource URI must be absolute: {uri}")
        parts = PurePosixPath(path).parts
        if ".." in parts:
            raise errors.AhpError(-32009, f"Not permitted to access {uri}")
        try:
            return PurePosixPath(path).relative_to(PurePosixPath(self.root))
        except ValueError:
            raise errors.AhpError(-32009, f"Not permitted to access {uri}") from None

    def _walk(self, relative: PurePosixPath) -> tuple[int, os.stat_result, PurePosixPath]:
        """Open the target without ever letting the kernel follow a symlink.

        Returns an open descriptor, its `fstat`, and the canonical path relative
        to the root. The caller closes the descriptor.
        """
        parts = [p for p in relative.parts if p not in ("", ".")]
        return self._walk_parts(parts, PurePosixPath(), 0)

    def _walk_parts(
        self, parts: Sequence[str], resolved: PurePosixPath, hops: int
    ) -> tuple[int, os.stat_result, PurePosixPath]:
        if hops > _MAX_SYMLINKS:
            raise errors.AhpError(-32009, "Too many symbolic links")

        fd = os.open(self.root, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_DIRECTORY", 0))
        try:
            for index, part in enumerate(parts):
                if part == "..":
                    raise errors.AhpError(-32009, "Not permitted to leave the resource root")
                # O_NONBLOCK matters as much as O_NOFOLLOW here: opening a
                # fifo O_RDONLY BLOCKS until a writer appears, so without it a
                # peer that can create a named pipe inside the root can hang
                # the host by naming it. Harmless on regular files.
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except OSError as exc:
                    # ELOOP is how O_NOFOLLOW reports "this is a symlink". The
                    # link is then resolved HERE, where the result can be
                    # re-checked against the root, rather than by the kernel,
                    # where it cannot.
                    if exc.errno in (errors.ELOOP, errors.EMLINK) and self._follow:
                        target = os.readlink(part, dir_fd=fd)
                        replacement = self._relink(target, resolved)
                        os.close(fd)
                        fd = -1
                        return self._walk_parts(
                            [*replacement, *parts[index + 1 :]], PurePosixPath(), hops + 1
                        )
                    raise self._not_found(exc) from exc
                os.close(fd)
                fd = child
                resolved = resolved / part
            return fd, os.fstat(fd), resolved
        except BaseException:
            # `fd` is -1 once the symlink branch has handed ownership on. A bare
            # `os.close` there would double-close, and on a busy host a
            # double-closed descriptor number gets reused -- so the second close
            # would shut an unrelated file, or somebody's socket.
            if fd >= 0:
                os.close(fd)
            raise

    def _relink(self, target: str, resolved: PurePosixPath) -> list[str]:
        """One symlink target, as a path to re-walk from the root.

        Restarting the walk is what makes following a link safe: the substituted
        path goes through exactly the same containment check as the original, so
        a link never buys an unchecked traversal.
        """
        if PurePosixPath(target).is_absolute():
            try:
                rest = PurePosixPath(target).relative_to(PurePosixPath(self.root))
            except ValueError:
                raise errors.AhpError(-32009, "Symbolic link leaves the resource root") from None
            replacement = list(rest.parts)
        else:
            replacement = [*resolved.parts, *PurePosixPath(target).parts]

        # `..` inside a link target is legitimate -- `../hello.txt` from a
        # subdirectory is an ordinary thing to write -- so it is collapsed here
        # rather than refused. Collapsing past the root is the escape, and that
        # is what the empty check catches.
        collapsed: list[str] = []
        for part in replacement:
            if part == "..":
                if not collapsed:
                    raise errors.AhpError(-32009, "Symbolic link leaves the resource root")
                collapsed.pop()
            elif part not in ("", "."):
                collapsed.append(part)
        return collapsed

    @staticmethod
    def _not_found(exc: OSError) -> errors.AhpError:
        if exc.errno in (errors.EACCES, errors.EPERM):
            return errors.AhpError(-32009, "Not permitted to access that resource")
        return errors.AhpError(-32008, "No such resource")

    def _canonical_uri(self, resolved: PurePosixPath) -> str:
        return uri_from_path(Path(self.root) / resolved)

    # ─── the provider surface ────────────────────────────────────────────

    async def resolve(self, uri: str, *, follow_symlinks: bool = True) -> ResourceInfo:
        relative = self._relative(uri)
        if not follow_symlinks:
            # lstat semantics: describe the link, do not traverse it. Answered
            # from the parent so the final component is never opened.
            info = self._lstat_without_following(relative, uri)
            if info is not None:
                return info
        fd, stats, resolved = self._walk(relative)
        try:
            return self._info(self._canonical_uri(resolved), stats, resolved)
        finally:
            os.close(fd)

    def _lstat_without_following(self, relative: PurePosixPath, uri: str) -> ResourceInfo | None:
        parts = [p for p in relative.parts if p not in ("", ".")]
        if not parts:
            return None
        fd, _stats, _resolved = self._walk_parts(parts[:-1], PurePosixPath(), 0)
        try:
            stats = os.lstat(parts[-1], dir_fd=fd)
        except OSError as exc:
            raise self._not_found(exc) from exc
        finally:
            os.close(fd)
        if not stat.S_ISLNK(stats.st_mode):
            return None
        return ResourceInfo(uri=uri, type="symlink", size=stats.st_size, mtime=_iso(stats.st_mtime))

    def _info(self, uri: str, stats: os.stat_result, resolved: PurePosixPath) -> ResourceInfo:
        directory = stat.S_ISDIR(stats.st_mode)
        content_type = None if directory else mimetypes.guess_type(resolved.name)[0]
        return ResourceInfo(
            uri=uri,
            type="directory" if directory else "file",
            size=None if directory else stats.st_size,
            mtime=_iso(stats.st_mtime),
            ctime=_iso(stats.st_ctime),
            content_type=content_type,
            # `st_mtime_ns` rather than a whole-millisecond mtime: two writes
            # inside one millisecond are otherwise indistinguishable, which is a
            # real lost-update window for the `ifMatch` flow an etag exists to
            # protect. Recorded as an upstream question -- the spec says only
            # "opaque per-provider version token".
            etag=None if directory else f'W/"{stats.st_size}-{stats.st_mtime_ns}"',
        )

    async def read(self, uri: str) -> ResourceContent:
        fd, stats, resolved = self._walk(self._relative(uri))
        try:
            if stat.S_ISDIR(stats.st_mode):
                raise errors.invalid_params(f"{uri} is a directory")
            if not stat.S_ISREG(stats.st_mode):
                # A device or fifo would block, or stream forever. Neither is a
                # resource a client asked for.
                raise errors.AhpError(-32009, "Not a regular file")
            with os.fdopen(os.dup(fd), "rb") as handle:
                data = handle.read()
        finally:
            os.close(fd)
        return ResourceContent(data=data, content_type=mimetypes.guess_type(resolved.name)[0])

    async def list_dir(self, uri: str) -> Sequence[DirectoryEntry]:
        fd, stats, _resolved = self._walk(self._relative(uri))
        try:
            if not stat.S_ISDIR(stats.st_mode):
                raise errors.invalid_params(f"{uri} is not a directory")
            entries: list[DirectoryEntry] = []
            for name in sorted(os.listdir(fd)):
                try:
                    child = os.lstat(name, dir_fd=fd)
                except OSError:
                    # Vanished between listing and stat. Skipped rather than
                    # failing the whole listing over one racing entry.
                    continue
                if stat.S_ISLNK(child.st_mode):
                    # Reported by what it points at, so a client sees the same
                    # `type` it would get from `resourceResolve`. A link out of
                    # the jail is unreadable and is listed as a file.
                    child = self._stat_link_target(fd, name) or child
                entries.append(
                    DirectoryEntry(
                        name=name,
                        type="directory" if stat.S_ISDIR(child.st_mode) else "file",
                    )
                )
            return entries
        finally:
            os.close(fd)

    def _stat_link_target(self, fd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=fd)
        except OSError:
            return None

    # ─── the write half ──────────────────────────────────────────────────

    def _writable(self, uri: str) -> None:
        if not self.writable:
            raise errors.AhpError(-32009, f"This host is read-only: {uri}")

    def _lock_for(self, uri: str) -> asyncio.Lock:
        """One lock per path.

        `append` "MUST evaluate the effective EOF and write atomically with
        respect to other appenders", and two appends that each read EOF before
        either writes will overwrite one another. This is per-process only --
        the spec's wording arguably reaches across processes, which is recorded
        as an upstream question in `docs/research.md`.
        """
        lock = self._locks.get(uri)
        if lock is None:
            lock = self._locks[uri] = asyncio.Lock()
        return lock

    def _parent_and_name(self, uri: str) -> tuple[int, str]:
        """An fd on the target's parent, and the target's base name.

        The parent is reached by the same walk as any read, so a write cannot
        escape the jail by any route a read could not.
        """
        relative = self._relative(uri)
        parts = [p for p in relative.parts if p not in ("", ".")]
        if not parts:
            raise errors.AhpError(-32009, "Not permitted to replace the resource root")
        fd, stats, _resolved = self._walk_parts(parts[:-1], PurePosixPath(), 0)
        if not stat.S_ISDIR(stats.st_mode):
            os.close(fd)
            raise errors.AhpError(-32008, "No such directory")
        return fd, parts[-1]

    async def write(
        self,
        uri: str,
        data: bytes,
        *,
        mode: str = "truncate",
        position: int = 0,
        create_only: bool = False,
        if_match: str | None = None,
    ) -> None:
        self._writable(uri)
        async with self._lock_for(uri):
            fd, name = self._parent_and_name(uri)
            try:
                self._write_locked(fd, name, uri, data, mode, position, create_only, if_match)
            finally:
                os.close(fd)

    def _write_locked(
        self,
        parent: int,
        name: str,
        uri: str,
        data: bytes,
        mode: str,
        position: int,
        create_only: bool,
        if_match: str | None,
    ) -> None:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        if create_only:
            flags |= os.O_EXCL
        try:
            target = os.open(name, flags, 0o600, dir_fd=parent)
        except OSError as exc:
            if exc.errno == _errno.EEXIST:
                raise errors.AhpError(-32010, f"Already exists: {uri}") from exc
            raise self._not_found(exc) from exc
        try:
            stats = os.fstat(target)
            if not stat.S_ISREG(stats.st_mode):
                raise errors.AhpError(-32009, "Not a regular file")
            if if_match is not None:
                current = f'W/"{stats.st_size}-{stats.st_mtime_ns}"'
                if current != if_match:
                    # -32011 Conflict. The whole point of `ifMatch`: somebody
                    # else wrote between the read and this write.
                    raise errors.AhpError(-32011, f"{uri} changed since it was read")
            self._place(target, stats.st_size, data, mode, position)
        finally:
            os.close(target)

    @staticmethod
    def _place(fd: int, size: int, data: bytes, mode: str, position: int) -> None:
        """Put `data` into the file according to `mode`.

        The three modes root `position` differently, which is easy to get
        backwards: `truncate` counts from the start, `append` counts **backwards
        from EOF**, and `insert` counts from the start but keeps the tail.
        """
        if mode == "append":
            offset = max(0, size - position)
            tail = os.pread(fd, size - offset, offset) if size > offset else b""
            os.pwrite(fd, data + tail, offset)
            os.ftruncate(fd, offset + len(data) + len(tail))
        elif mode == "insert":
            offset = min(position, size)
            tail = os.pread(fd, size - offset, offset) if size > offset else b""
            os.pwrite(fd, data + tail, offset)
            os.ftruncate(fd, offset + len(data) + len(tail))
        else:
            offset = min(position, size)
            os.ftruncate(fd, offset)
            os.pwrite(fd, data, offset)
            os.ftruncate(fd, offset + len(data))

    async def mkdir(self, uri: str) -> None:
        """`mkdir -p`: missing parents are created, an existing directory is a
        no-op success, and an existing non-directory is `AlreadyExists`."""
        self._writable(uri)
        relative = self._relative(uri)
        parts = [p for p in relative.parts if p not in ("", ".")]
        built = PurePosixPath()
        for part in parts:
            fd, _stats, _resolved = self._walk_parts(list(built.parts), PurePosixPath(), 0)
            try:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    existing = os.lstat(part, dir_fd=fd)
                    if not stat.S_ISDIR(existing.st_mode):
                        raise errors.AhpError(-32010, f"Already exists: {uri}") from None
                except OSError as exc:
                    raise self._not_found(exc) from exc
            finally:
                os.close(fd)
            built = built / part

    async def delete(self, uri: str, *, recursive: bool = False) -> None:
        self._writable(uri)
        async with self._lock_for(uri):
            fd, name = self._parent_and_name(uri)
            try:
                stats = os.lstat(name, dir_fd=fd)
                if stat.S_ISDIR(stats.st_mode):
                    if not recursive:
                        os.rmdir(name, dir_fd=fd)
                        return
                    # `shutil.rmtree` needs a path, so the jail is re-verified
                    # by walking to it first and only then handing over the
                    # canonical location.
                    target, _s, resolved = self._walk_parts(
                        [*self._relative(uri).parts], PurePosixPath(), 0
                    )
                    os.close(target)
                    shutil.rmtree(Path(self.root) / resolved)
                else:
                    os.unlink(name, dir_fd=fd)
            except OSError as exc:
                if exc.errno == _errno.ENOTEMPTY:
                    raise errors.invalid_params(f"{uri} is not empty") from exc
                raise self._not_found(exc) from exc
            finally:
                os.close(fd)

    async def move(self, source: str, destination: str, *, fail_if_exists: bool = False) -> None:
        self._writable(destination)
        source_fd, source_name = self._parent_and_name(source)
        try:
            dest_fd, dest_name = self._parent_and_name(destination)
        except BaseException:
            os.close(source_fd)
            raise
        try:
            self._guard_destination(dest_fd, dest_name, destination, fail_if_exists)
            os.rename(source_name, dest_name, src_dir_fd=source_fd, dst_dir_fd=dest_fd)
        except OSError as exc:
            raise self._not_found(exc) from exc
        finally:
            os.close(source_fd)
            os.close(dest_fd)

    async def copy(self, source: str, destination: str, *, fail_if_exists: bool = False) -> None:
        self._writable(destination)
        content = await self.read(source)
        dest_fd, dest_name = self._parent_and_name(destination)
        try:
            self._guard_destination(dest_fd, dest_name, destination, fail_if_exists)
        finally:
            os.close(dest_fd)
        await self.write(destination, content.data)

    @staticmethod
    def _guard_destination(parent: int, name: str, uri: str, fail_if_exists: bool) -> None:
        if not fail_if_exists:
            return
        try:
            os.lstat(name, dir_fd=parent)
        except OSError:
            return
        raise errors.AhpError(-32010, f"Already exists: {uri}")
