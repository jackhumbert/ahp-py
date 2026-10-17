"""Find a Node binary new enough to run the script sandbox."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Final

MIN_NODE_MAJOR: Final = 20
NODE_ENV: Final = "AHP_FLOW_NODE"


class NodeNotFoundError(RuntimeError):
    """No usable Node binary was found."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message or f"no Node >= {MIN_NODE_MAJOR} found: set {NODE_ENV} or put node on PATH"
        )


def parse_node_version(text: str) -> tuple[int, int, int] | None:
    """Parse the output of `node --version`; None if it is not a version."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def node_version(path: str) -> tuple[int, int, int] | None:
    """Run `path --version`; None if it cannot be run or prints no version."""
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return parse_node_version(proc.stdout)


def find_node(explicit: str | None = None) -> str | None:
    """Return a Node >= `MIN_NODE_MAJOR`, or None.

    Order: `explicit`, then `AHP_FLOW_NODE`, then `node` on `PATH`. A value
    that is set but unusable does not fall back, so a bad setting is noticed.
    """
    candidate: str | None = explicit
    if candidate is None:
        candidate = os.environ.get(NODE_ENV) or shutil.which("node")
    if candidate is None:
        return None
    version = node_version(candidate)
    if version is None or version[0] < MIN_NODE_MAJOR:
        return None
    return candidate
