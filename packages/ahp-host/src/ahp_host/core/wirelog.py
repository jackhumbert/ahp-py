"""Frame logging in ahp-inspector's JSONL format.

Each line is the JSON-RPC message, **with credentials redacted**, plus one
root-level sidecar:

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

## Redaction

`authenticate` carries a live bearer token in `params.token`, and this file is
written unencrypted to a path the user chose for debugging. So every frame is
walked and any value under a credential-shaped key is replaced with a marker
before it is written.

Three properties this deliberately keeps:

* **The frame's shape survives.** A redacted value becomes a string marker, not
  a deletion, so a reader still sees that the field was present. Debugging an
  auth handshake stays possible; the secret does not reach disk.
* **The outbound frame is untouched.** Redaction rebuilds containers rather than
  mutating them — the mapping passed in is aliased by the message on its way to
  the socket.
* **There is no opt-out.** A flag to log credentials verbatim is a flag someone
  eventually sets on a machine they do not control.

The key list is deliberately broader than AHP's own surface (`token` is the only
one the protocol defines today) because a provider adapter may put its own
credentials in `_meta`, and a log is the wrong place to discover that it did.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

__all__ = ["REDACTED", "WireLog", "redact"]

#: Replaces a credential value. Distinctive enough to grep a log for.
REDACTED: Final = "<redacted by ahp-host>"

#: Matched case-insensitively against object keys, at any depth. `token` is
#: `AuthenticateParams.token`; `tkn` is VS Code's connection-token query
#: parameter, which reaches us only via a handshake path but is cheap to cover.
_SENSITIVE_KEYS: Final = frozenset(
    {
        "access_token",
        "accesstoken",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "clientsecret",
        "connectiontoken",
        "credentials",
        "id_token",
        "idtoken",
        "password",
        "refresh_token",
        "refreshtoken",
        "secret",
        "tkn",
        "token",
    }
)


def redact(value: Any) -> Any:
    """Return `value` with every credential-shaped field replaced.

    Rebuilds containers rather than mutating them: the caller's mapping is the
    live outbound frame.
    """
    if isinstance(value, Mapping):
        return {
            key: REDACTED
            if isinstance(key, str) and key.lower().replace("-", "_") in _SENSITIVE_KEYS
            else redact(item)
            for key, item in value.items()
        }
    # `str` and `bytes` are Sequences; only real lists and tuples recurse.
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [redact(item) for item in value]
    return value


class WireLog:
    """Appends frames to a JSONL file. Best-effort: never breaks the connection."""

    def __init__(self, path: Path, *, transport: str = "ws") -> None:
        self.path = path
        self.transport = transport
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate on open so a run's log is its own, matching how the VS Code
        # host writes a fresh log per agent-host process. Owner-only: a wire log
        # is a full transcript of every session on the host, and `record`
        # redacts credentials but cannot redact the conversation.
        self._handle = self.path.open("w", encoding="utf-8")
        # Windows, or a filesystem with no mode bits: not worth failing over.
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    def record(self, direction: str, message: Mapping[str, Any], connection_id: str) -> None:
        if self._handle.closed:
            return
        entry: dict[str, Any] = redact(message)
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
