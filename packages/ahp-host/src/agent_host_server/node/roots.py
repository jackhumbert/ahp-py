"""The folders a host serves: one unnamed root, or several named ones.

One unnamed root (`--root PATH`) is served as itself: its `file:` URIs are the
real paths, as before.

Named roots (`[roots]` in the config file, or `--root NAME=PATH`) are served as
a small tree of the host's own: `file:///` lists the names, and
`file:///<name>/<rel>` is `<rel>` under that root. Nothing else on the machine
is reachable, and each root is still served through agent-host-server's jail
(`RootedFilesystemResourceProvider`, POSIX or Windows), so the combined view
only ever *chooses* a jail - it never opens a path itself.

Behind a broker the tree shows up as `<machine>/<name>/...`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from agent_host_protocol import errors

from agent_host_server.core.resources import (
    DirectoryEntry,
    ResourceContent,
    ResourceInfo,
    RootedFilesystemResourceProvider,
)
from agent_host_server.node.paths import directory_of

#: A root's name becomes a path segment every client shows; keep it plain.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TOP = "file:///"


def is_valid_root_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None and name not in (".", "..")


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@dataclass(frozen=True)
class Roots:
    """The served folders. `named` is empty for a single unnamed root."""

    paths: tuple[Path, ...]
    names: tuple[str, ...] = ()

    @classmethod
    def single(cls, root: Path) -> Roots:
        return cls((Path(root).resolve(),))

    @classmethod
    def named(cls, roots: Mapping[str, Path]) -> Roots:
        if not roots:
            raise ValueError("at least one root is required")
        for name in roots:
            if not is_valid_root_name(name):
                raise ValueError(f"root name {name!r}: use letters, digits, '.', '-', '_'")
        paths = tuple(Path(path).expanduser().resolve() for path in roots.values())
        return cls(paths, tuple(roots))

    @property
    def is_named(self) -> bool:
        return bool(self.names)

    @property
    def primary(self) -> Path:
        """Where a session with no folder starts."""
        return self.paths[0]

    def contains(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(_inside(resolved, root) for root in self.paths)

    def default_directory(self) -> str:
        """What the host advertises for a folder picker to open at."""
        return _TOP if self.is_named else self.primary.as_uri()

    # -- URI <-> real path ------------------------------------------------------

    def _split(self, uri: str) -> tuple[int, str] | None:
        """For a named tree URI: (root index, relative path); None if not one."""
        parsed = urlparse(uri)
        if parsed.scheme not in ("file", "vscode-agent-host"):
            return None
        segments = [unquote(s) for s in parsed.path.split("/") if s]
        if not segments or segments[0] not in self.names:
            return None
        if ".." in segments:
            return None
        return self.names.index(segments[0]), "/".join(segments[1:])

    def real_path(self, uri: str) -> Path | None:
        """A client's folder or file URI as a real path inside a root, or None.

        Named: `file:///<name>/<rel>`; the top (`file:///`) means the primary
        root. Unnamed: the URI's own path, if it lies inside the root.
        """
        if self.is_named:
            parsed = urlparse(uri)
            if parsed.scheme in ("file", "vscode-agent-host") and not parsed.path.strip("/"):
                return self.primary
            split = self._split(uri)
            if split is None:
                return None
            index, rel = split
            real = (self.paths[index] / rel).resolve() if rel else self.paths[index]
            return real if _inside(real, self.paths[index]) else None
        path = directory_of(uri)
        if path is None:
            return None
        resolved = path.resolve()
        return resolved if self.contains(resolved) else None

    def tree_uri(self, real: Path) -> str | None:
        """A real path back to the URI a client sees; None if outside every root."""
        if not self.is_named:
            return real.as_uri() if self.contains(real) else None
        resolved = real.resolve()
        for name, root in zip(self.names, self.paths, strict=True):
            if _inside(resolved, root):
                rel = resolved.relative_to(root).as_posix()
                suffix = f"/{quote(rel)}" if rel and rel != "." else ""
                return f"{_TOP}{quote(name)}{suffix}"
        return None


class NamedRootsResourceProvider:
    """A read-only view of several jailed roots under one small tree.

    Deliberately has no `root` attribute: the host would compare working
    directories against it. It answers `serves` instead, which the host asks.
    """

    def __init__(self, roots: Roots) -> None:
        if not roots.is_named:
            raise ValueError("NamedRootsResourceProvider needs named roots")
        self.roots = roots
        self._jails = tuple(RootedFilesystemResourceProvider(path) for path in roots.paths)

    def serves(self, uri: str) -> bool:
        return self.roots.real_path(uri) is not None

    def _jail_uri(self, uri: str) -> tuple[RootedFilesystemResourceProvider, str]:
        split = self.roots._split(uri)
        if split is None:
            raise errors.AhpError(-32008, f"No such resource: {uri}")
        index, rel = split
        base = self.roots.paths[index].as_uri()
        return self._jails[index], (f"{base}/{quote(rel)}" if rel else base)

    def _to_tree(self, info: ResourceInfo) -> ResourceInfo:
        path = directory_of(info.uri)
        tree = self.roots.tree_uri(path) if path is not None else None
        if tree is None:
            # The jail answered with a path outside every root: never show it.
            raise errors.AhpError(-32009, "Not permitted")
        return ResourceInfo(
            uri=tree,
            type=info.type,
            size=info.size,
            mtime=info.mtime,
            ctime=info.ctime,
            content_type=info.content_type,
            etag=info.etag,
        )

    def _is_top(self, uri: str) -> bool:
        parsed = urlparse(uri)
        return parsed.scheme in ("file", "vscode-agent-host") and not parsed.path.strip("/")

    async def resolve(self, uri: str, *, follow_symlinks: bool = True) -> ResourceInfo:
        if self._is_top(uri):
            return ResourceInfo(uri=_TOP, type="directory")
        jail, real = self._jail_uri(uri)
        return self._to_tree(await jail.resolve(real, follow_symlinks=follow_symlinks))

    async def read(self, uri: str) -> ResourceContent:
        if self._is_top(uri):
            raise errors.AhpError(-32009, "The list of folders is not a file")
        jail, real = self._jail_uri(uri)
        return await jail.read(real)

    async def list_dir(self, uri: str) -> Sequence[DirectoryEntry]:
        if self._is_top(uri):
            return [DirectoryEntry(name=name, type="directory") for name in self.roots.names]
        jail, real = self._jail_uri(uri)
        return await jail.list_dir(real)


def as_roots(value: Path | Roots) -> Roots:
    """Accept a plain root where Roots are expected (one unnamed root)."""
    return value if isinstance(value, Roots) else Roots.single(value)


def parse_root_arg(value: str) -> tuple[str | None, Path]:
    """`--root PATH` or `--root NAME=PATH`. A Windows drive (`C:\\x`) is a path, not a name."""
    name, sep, path = value.partition("=")
    if sep and is_valid_root_name(name) and not re.fullmatch(r"[A-Za-z]", name):
        return name, Path(path)
    return None, Path(value)
