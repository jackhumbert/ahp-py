"""Where a stable ``clientId`` lives across process restarts.

``reconnect`` identifies a logical client by its ``clientId``. An app that wants
to resume an in-progress turn after the user kills it has to persist that id
somewhere durable -- otherwise every launch is a new client to the host, and
every launch takes fresh snapshots.

The on-disk format is Rust's and Swift's, which is the one two reference SDKs
agree on: one file per host, raw UTF-8, no trailing newline, `0o600`.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

__all__ = ["ClientIdStore", "FileClientIdStore", "InMemoryClientIdStore"]


class ClientIdStore(Protocol):
    """Persistence for per-host client ids."""

    async def load(self, host_id: str) -> str | None: ...

    async def store(self, host_id: str, client_id: str) -> None: ...


class InMemoryClientIdStore:
    """Stable within a process, gone on restart. Fine for tests and short CLIs."""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}

    async def load(self, host_id: str) -> str | None:
        return self._ids.get(host_id)

    async def store(self, host_id: str, client_id: str) -> None:
        self._ids[host_id] = client_id


class FileClientIdStore:
    """One ``<percent-encoded-host-id>.clientid`` file per host.

    Writes are temp-file-plus-rename so a crash mid-write cannot leave a
    truncated id -- an id that half-survives is worse than one that does not,
    because the host will not recognise it and the client will not know why.

    **Last writer wins across processes.** Two instances of an app pointed at
    the same directory will fight over the id; that is inherent to the format
    and is the same in the Rust and Swift stores.
    """

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self._directory = Path(directory)

    def _path(self, host_id: str) -> Path:
        return self._directory / f"{quote(host_id, safe='')}.clientid"

    async def load(self, host_id: str) -> str | None:
        try:
            raw = self._path(host_id).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return None
        # Deliberately not stripped -- Rust's rule. Only a fully empty file
        # counts as absent, so an id someone padded is still the id they stored.
        return raw or None

    async def store(self, host_id: str, client_id: str) -> None:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self._path(host_id)
        handle, temporary = tempfile.mkstemp(dir=self._directory, prefix=".clientid-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(client_id)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            with suppress_os_error():
                os.unlink(temporary)
            raise


class suppress_os_error:  # noqa: N801 - a context manager, named for the call site
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)
