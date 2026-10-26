"""The folders a host serves: one unnamed root, or several named ones.

One unnamed root (`--root PATH`) is served as itself: its `file:` URIs are the
real paths, as before.

Named roots (`[roots]` in the config file, or `--root NAME=PATH`) are served as
a small tree of the host's own: `file:///` lists the names, and
`file:///<name>/<rel>` is `<rel>` under that root. Nothing else on the machine
is reachable, and each root is still served through ahp-host's jail
(`RootedFilesystemResourceProvider`, POSIX or Windows), so the combined view
only ever *chooses* a jail - it never opens a path itself.

Behind a gateway the tree shows up as `<machine>/<name>/...`.

Named roots can instead be served at their real paths (`layout="real"`,
`--roots-layout real`): each root is its real absolute `file:` URI, and a
picker can walk down from `file:///` through the *spine* - the directories on
the way to a root (`/`, `/home`, `/home/dev` for a root `/home/dev/proj`). A
spine directory lists only the children that are roots or lead to one, and the
listing is synthesized from the roots, never read from the disk. It is a
listing and nothing more: reading, writing or watching a spine path is refused
like any path outside the roots. Tree spellings (`file:///<name>/<rel>`) still
resolve to the named root, unless the URI is itself inside a real root, on the
spine, or names a root that is also a real top-level folder (`file:///tmp/x` is
`/tmp/x`, not a root named `tmp`): the real path wins. Which names those are is
decided once, when the roots are built (logged as a warning), so a URI resolves
the same way for the life of the process and resolving one never reads the disk.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from ahp_protocol import errors

from ahp_host.core.resources import (
    DirectoryEntry,
    ResourceContent,
    ResourceInfo,
    RootedFilesystemResourceProvider,
)
from ahp_host.node.paths import directory_of

log = logging.getLogger(__name__)

#: A root's name becomes a path segment every client shows; keep it plain.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TOP = "file:///"
LAYOUTS = ("tree", "real")


def layout_of(flag: str | None, file_value: object = "tree") -> str:
    """The roots layout: the flag, else the config file's `roots_layout`, else "tree".

    Raises ValueError for anything but one of `LAYOUTS`.
    """
    layout = flag or file_value
    if not isinstance(layout, str) or layout not in LAYOUTS:
        raise ValueError(f"roots_layout must be one of: {', '.join(LAYOUTS)}")
    return layout


def is_valid_root_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None and name not in (".", "..")


def _is_top_level_folder(anchor: str, name: str) -> bool:
    """Whether the machine has a real top-level folder `<anchor><name>`."""
    try:
        return Path(anchor, name).is_dir()
    except OSError:
        return False


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@dataclass(frozen=True)
class Roots:
    """The served folders. `named` is empty for a single unnamed root."""

    paths: tuple[Path, ...]
    names: tuple[str, ...] = ()
    #: "tree": named roots under a virtual tree. "real": at their real URIs.
    layout: str = "tree"
    #: Real layout: root names that are also a real top-level folder, so their
    #: tree spelling is not read (`file:///tmp/x` is `/tmp/x`). Decided once,
    #: when the roots are built.
    shadowed: frozenset[str] = frozenset()

    @classmethod
    def single(cls, root: Path) -> Roots:
        return cls((Path(root).resolve(),))

    @classmethod
    def named(cls, roots: Mapping[str, Path], layout: str = "tree") -> Roots:
        if layout not in LAYOUTS:
            raise ValueError(f"roots layout {layout!r}: use one of {', '.join(LAYOUTS)}")
        if not roots:
            raise ValueError("at least one root is required")
        for name in roots:
            if not is_valid_root_name(name):
                raise ValueError(f"root name {name!r}: use letters, digits, '.', '-', '_'")
        paths = tuple(Path(path).expanduser().resolve() for path in roots.values())
        shadowed: frozenset[str] = frozenset()
        if layout == "real":
            anchors = {path.anchor for path in paths}
            shadowed = frozenset(
                name
                for name in roots
                if any(_is_top_level_folder(anchor, name) for anchor in anchors)
            )
            for name in sorted(shadowed):
                log.warning(
                    "root %r: the machine has a top-level folder of that name, so"
                    " file:///%s/... is that folder, not this root",
                    name,
                    name,
                )
        return cls(paths, tuple(roots), layout, shadowed)

    @property
    def is_named(self) -> bool:
        return bool(self.names)

    @property
    def is_real(self) -> bool:
        """Named roots served at their real paths (the sparse real tree)."""
        return self.is_named and self.layout == "real"

    @property
    def primary(self) -> Path:
        """Where a session with no folder starts."""
        return self.paths[0]

    def contains(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(_inside(resolved, root) for root in self.paths)

    def default_directory(self) -> str:
        """What the host advertises for a folder picker to open at."""
        return _TOP if self.is_named and not self.is_real else self.primary.as_uri()

    # -- the spine (real layout) --------------------------------------------------

    def _chains(self) -> list[tuple[Path, ...]]:
        """Each root's path from its anchor down to the root itself."""
        return [(*reversed(root.parents), root) for root in self.paths]

    def _spine_node(self, uri: str) -> tuple[Path | None] | None:
        """The spine directory a URI names, as a one-tuple; None if it is not on
        the spine. The tuple holds None for the top of a Windows machine (above
        its drives)."""
        parsed = urlparse(uri)
        if parsed.scheme not in ("file", "vscode-agent-host"):
            return None
        if not parsed.path.strip("/") and os.name == "nt":
            return (None,)
        path = directory_of(uri)
        if path is None or ".." in path.parts:
            return None
        if any(_inside(path, root) for root in self.paths):
            return None
        for chain in self._chains():
            if path in chain[:-1]:
                return (path,)
        return None

    def spine_children(self, uri: str) -> list[str] | None:
        """Real layout: the names to list under a spine directory, or None.

        Only the children that are roots or lead toward one; never the disk.
        """
        if not self.is_real:
            return None
        spine = self._spine_node(uri)
        if spine is None:
            return None
        node = spine[0]
        names: set[str] = set()
        for chain in self._chains():
            if node is None:
                child: Path | None = chain[0]
            else:
                child = next((b for a, b in pairwise(chain) if a == node), None)
            if child is not None:
                names.add(child.name or child.drive or str(child))
        return sorted(names)

    def spine_uri(self, uri: str) -> str | None:
        """The canonical URI of a spine directory, or None if not on the spine."""
        if not self.is_real:
            return None
        spine = self._spine_node(uri)
        if spine is None:
            return None
        node = spine[0]
        return _TOP if node is None else node.as_uri()

    # -- URI <-> real path ------------------------------------------------------

    def _split(self, uri: str) -> tuple[int, str] | None:
        """For a root's URI: (root index, relative path); None if not one.

        Tree layout: `file:///<name>/<rel>`. Real layout: the real path inside
        a root first, then (a real path wins) the tree spelling, unless the URI
        is on the spine or its name is `shadowed` by a real top-level folder.
        """
        parsed = urlparse(uri)
        if parsed.scheme not in ("file", "vscode-agent-host"):
            return None
        if self.is_real:
            path = directory_of(uri)
            if path is not None and ".." not in path.parts:
                for index, root in enumerate(self.paths):
                    if _inside(path, root):
                        rel = "" if path == root else path.relative_to(root).as_posix()
                        return index, rel
            if self._spine_node(uri) is not None:
                return None
        segments = [unquote(s) for s in parsed.path.split("/") if s]
        if not segments or segments[0] not in self.names:
            return None
        if segments[0] in self.shadowed:
            # A real top-level folder (`/tmp`) is that path, not the root a
            # tree spelling would call `tmp`.
            return None
        if ".." in segments:
            return None
        return self.names.index(segments[0]), "/".join(segments[1:])

    def real_path(self, uri: str) -> Path | None:
        """A client's folder or file URI as a real path inside a root, or None.

        Named: `file:///<name>/<rel>`; the top (`file:///`) means the primary
        root. Unnamed: the URI's own path, if it lies inside the root.

        A named tree also takes a plain absolute URI inside one of its roots
        (`file:///G:/llm`): what a session stored before its node's roots were
        named, or what a client sends that never saw the tree. A tree name wins
        where the two spellings could collide.
        """
        if self.is_named:
            parsed = urlparse(uri)
            if self.is_real and self._spine_node(uri) is not None:
                return None
            if (
                not self.is_real
                and parsed.scheme in ("file", "vscode-agent-host")
                and not parsed.path.strip("/")
            ):
                return self.primary
            split = self._split(uri)
            if split is not None:
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
        if self.is_real:
            return resolved.as_uri() if self.contains(resolved) else None
        for name, root in zip(self.names, self.paths, strict=True):
            if _inside(resolved, root):
                rel = resolved.relative_to(root).as_posix()
                suffix = f"/{quote(rel)}" if rel and rel != "." else ""
                return f"{_TOP}{quote(name)}{suffix}"
        return None


class NamedRootsResourceProvider:
    """A read-only view of several jailed roots under one small tree.

    In the real layout the roots sit at their real URIs and the only thing
    added is the spine: a listing-only chain of directories down from the top
    (`Roots.spine_children`). `serves` stays False for it, so the host grants
    no working directory, watch or read there.

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
            # A plain absolute URI inside a root (`serves` says yes to those):
            # its tree spelling, so the read still walks that root's jail.
            real = self.roots.real_path(uri)
            tree = self.roots.tree_uri(real) if real is not None else None
            split = self.roots._split(tree) if tree is not None else None
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
        spine = self.roots.spine_uri(uri)
        if spine is not None:
            return ResourceInfo(uri=spine, type="directory")
        if self._is_top(uri) and not self.roots.is_real:
            return ResourceInfo(uri=_TOP, type="directory")
        jail, real = self._jail_uri(uri)
        return self._to_tree(await jail.resolve(real, follow_symlinks=follow_symlinks))

    async def read(self, uri: str) -> ResourceContent:
        if self._is_top(uri) and not self.roots.is_real:
            raise errors.AhpError(-32009, "The list of folders is not a file")
        jail, real = self._jail_uri(uri)
        return await jail.read(real)

    async def list_dir(self, uri: str) -> Sequence[DirectoryEntry]:
        spine = self.roots.spine_children(uri)
        if spine is not None:
            return [DirectoryEntry(name=name, type="directory") for name in spine]
        if self._is_top(uri) and not self.roots.is_real:
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
