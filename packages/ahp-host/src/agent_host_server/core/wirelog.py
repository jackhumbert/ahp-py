"""Frame logging in ahp-inspector's JSONL format.

Each line is the **verbatim** JSON-RPC message plus one root-level sidecar:

    {"jsonrpc":"2.0", ..., "_ahpLog":{"ts":"<ISO8601>","dir":"c2s"|"s2c", ...}}

Only `ts` and `dir` are consumed by the reader; `connectionId` and `transport`
appear in real fixtures and are ignored. Direction is from the *client's* point
of view: `c2s` is client-to-host.

Emitting `dir` explicitly matters. The reader's fallback inference classifies
any method other than `action`/`notification` as `c2s`, so every modern
server-originated notification (`root/sessionAdded`, `auth/required`, ...) would
be mislabelled without it.

Name the file `agent-host-*.jsonl` and `npx ahp-inspector` discovers it with no
arguments. Note the inspector is a debugging convenience, not an oracle: its
normalizer still expects the pre-0.4 `{method:"notification"}` envelope, so
current notifications render as a generic row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["WireLog"]


class WireLog:
    """Appends frames to a JSONL file. Best-effort: never breaks the connection."""

    def __init__(self, path: Path, *, transport: str = "ws") -> None:
        self.path = path
        self.transport = transport
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate on open so a run's log is its own, matching how the VS Code
        # host writes a fresh log per agent-host process.
        self._handle = self.path.open("w", encoding="utf-8")

    def record(self, direction: str, message: Mapping[str, Any], connection_id: str) -> None:
        if self._handle.closed:
            return
        entry: dict[str, Any] = dict(message)
        entry["_ahpLog"] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "dir": direction,
            "connectionId": connection_id,
            "transport": self.transport,
        }
        try:
            self._handle.write(json.dumps(entry) + "\n")
            self._handle.flush()
        except (OSError, TypeError, ValueError):
            # A frame we cannot serialise must not take the session down.
            pass

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
