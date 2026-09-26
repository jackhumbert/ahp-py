"""The reverse direction: answering the host, and executing its tool calls.

A layer of equal weight to the forward direction (ADR 0006), because the
protocol is symmetric and no reference client finishes this half.
"""

from __future__ import annotations

from ahp_client.serve.inputs import (
    ClientToolHost,
    InputResponder,
    ToolExecutor,
    pending_inputs,
)
from ahp_client.serve.resources import (
    FileResourceServer,
    VirtualResourceServer,
    file_uri,
    path_from_uri,
)
from ahp_client.serve.router import REVERSE_METHODS, ResourceRouter, ResourceServer

__all__ = [
    "REVERSE_METHODS",
    "ClientToolHost",
    "FileResourceServer",
    "InputResponder",
    "ResourceRouter",
    "ResourceServer",
    "ToolExecutor",
    "VirtualResourceServer",
    "file_uri",
    "path_from_uri",
    "pending_inputs",
]
