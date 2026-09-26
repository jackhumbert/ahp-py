"""Protocol URIs as local paths, on POSIX and Windows alike."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlparse

#: `file:///C:/Users/me` carries the drive after a leading slash.
_DRIVE = re.compile(r"^/[A-Za-z]:")


def local_path_of(uri: str, *, windows: bool) -> PurePath | None:
    """A file or folder URI as a path in the given OS's flavour, or None.

    `file://` is the protocol's own form. `vscode-agent-host://<authority>/path`
    is what VS Code's folder browser sends for a remote host: the same path on
    this machine, under the host's name, so its authority is ignored. A `file`
    URI's authority is ignored too: a gateway in front of this host has already
    stripped its node name from it, and nothing else should put one there.
    """
    parsed = urlparse(uri)
    if parsed.scheme not in ("file", "vscode-agent-host") or not parsed.path:
        return None
    path = unquote(parsed.path)
    if not windows:
        return PurePosixPath(path)
    if _DRIVE.match(path):
        path = path[1:]
    return PureWindowsPath(path)


def directory_of(uri: str) -> Path | None:
    """`local_path_of` for the OS this host runs on."""
    path = local_path_of(uri, windows=os.name == "nt")
    return Path(path) if path is not None else None
