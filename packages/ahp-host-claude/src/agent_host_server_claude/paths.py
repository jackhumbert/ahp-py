"""Protocol URIs as local paths."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse


def directory_of(uri: str) -> Path | None:
    """A file or folder URI as a local path, or None if it is not one.

    `file://` is the protocol's own form. `vscode-agent-host://<authority>/path`
    is what VS Code's folder browser sends for a remote host: the same path on
    this machine, under the host's name.
    """
    parsed = urlparse(uri)
    if parsed.scheme in ("file", "vscode-agent-host") and parsed.path:
        return Path(unquote(parsed.path))
    return None
