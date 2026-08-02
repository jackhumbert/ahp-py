"""ahp-inspector-compatible wire logs.

One JSON-RPC message per UTF-8 line, verbatim, plus a root-level ``_ahpLog``
sidecar. The inspector discovers files matching
``/^(agenthost|agent-host|ahp).*\\.jsonl$/i`` with no arguments, so the default
filename is chosen to be found.

**Credentials are redacted, with no opt-out.** A flag to log them verbatim is a
flag somebody eventually sets on a machine they do not control -- and the client
is the party that sends ``authenticate{token}``, so this matters more on this
side than on the host's. The log still contains every message of every session,
which is a transcript, not a trace.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final, Literal

from agent_host_protocol.transport import Transport
from agent_host_protocol.types import JsonObject

__all__ = ["WireLog", "logged", "read_jsonl"]

#: Key names whose values never reach the file, at any depth. Matched
#: case-insensitively on a substring, because hosts spell these several ways.
_CREDENTIAL_KEYS: Final = ("token", "secret", "password", "authorization", "apikey", "api_key")

_REDACTED: Final = "<redacted>"


def _redact(value: Any) -> Any:
    """Copy *value*, replacing credential-shaped leaves.

    Containers are rebuilt rather than mutated: the outbound frame is about to
    be sent, and redacting in place would send the redaction.
    """
    if isinstance(value, Mapping):
        return {
            key: (
                _REDACTED
                if any(marker in str(key).lower() for marker in _CREDENTIAL_KEYS)
                else _redact(inner)
            )
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class WireLog:
    """Appends frames to a file. Owner-readable only."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        connection_id: str = "client",
        transport: str = "websocket",
    ) -> None:
        self.path = Path(path) if path is not None else Path(f"ahp-client-{os.getpid()}.jsonl")
        self._connection_id = connection_id
        self._transport = transport
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Created before the first write so the mode is right from the start,
        # not after a frame has already landed world-readable.
        self.path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)

    def record(self, direction: Literal["c2s", "s2c"], message: Mapping[str, Any]) -> None:
        entry: JsonObject = dict(_redact(message))
        entry["_ahpLog"] = {
            "ts": int(time.time() * 1000),
            "dir": direction,
            "connectionId": self._connection_id,
            "transport": self._transport,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")


class logged:  # noqa: N801 - a decorator-shaped wrapper, named for the call site
    """Wrap a transport so every frame is recorded on its way past."""

    def __init__(self, transport: Transport, log: WireLog) -> None:
        self._inner = transport
        self._log = log

    async def send(self, message: Mapping[str, Any]) -> None:
        self._log.record("c2s", message)
        await self._inner.send(message)

    async def receive(self) -> dict[str, Any] | None:
        message = await self._inner.receive()
        if message is not None:
            self._log.record("s2c", message)
        return message

    async def close(self) -> None:
        await self._inner.close()


def read_jsonl(path: str | os.PathLike[str]) -> Iterator[JsonObject]:
    """Read a wire log back.

    Tolerates a BOM (stripped **once**, at the start of the file only), CRLF
    line endings, and blank lines. A line that will not parse is skipped rather
    than ending the read -- a truncated final line is what a log looks like when
    the process was killed, which is exactly when you want to read it.
    """
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                yield parsed
