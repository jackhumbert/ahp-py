"""Host-owned copies of client plugins, for automations (1.0.0).

An automation runs when no client is connected, so the plugins its session
template names (`AutomationSessionTemplate.customizations`) cannot be read
from a client at run time. The host captures a copy when the definition is
saved instead -- reading `virtual://` and other client-served URIs from the
dispatching client with server→client `resourceList` / `resourceRead` -- and
serves that copy under its own URI for as long as the automation exists.

The walk is bounded, because the client is untrusted and decides what the
plugin contains: a client answering every `resourceList` with another
directory must not be able to make the host loop, or store gigabytes. A walk
that hits a limit **fails** the capture, and "if a capture fails, the host
rejects the whole action" -- a partial copy would run unattended without parts
the user saved.

Copies are JSON-safe (bytes as base64) so `FileAutomationStore` persists them
with the record they belong to.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote, unquote

__all__ = [
    "COPY_SCHEME",
    "CaptureError",
    "PluginCopy",
    "capture_plugin",
    "copy_root",
]

#: URIs of captured content: `ahp-plugin-copy:/<automation>/<plugin id>/<path>`.
COPY_SCHEME: Final = "ahp-plugin-copy:"

#: Generous for a plugin -- prompts, skills, agents are small text files -- and
#: small enough that a hostile client cannot fill the host's disk through it.
MAX_FILES: Final = 500
MAX_BYTES: Final = 8 * 1024 * 1024
MAX_DEPTH: Final = 8


class CaptureError(Exception):
    """A plugin could not be copied; the action naming it is rejected."""


@dataclass
class PluginCopy:
    """One captured plugin: what the template entry said, and its files."""

    plugin_id: str
    source_uri: str
    name: str
    nonce: str | None = None
    #: Relative path -> bytes. A file-shaped plugin (the client's `uri` is a
    #: file, not a directory) holds one entry under its file name.
    files: dict[str, bytes] = field(default_factory=dict)
    single_file: bool = False

    def matches(self, entry: Mapping[str, Any]) -> bool:
        """Whether *entry* still names this copy: same `id`, `uri` and `nonce`."""
        return (
            entry.get("id") == self.plugin_id
            and entry.get("uri") == self.source_uri
            and entry.get("nonce") == self.nonce
        )

    def to_json(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "id": self.plugin_id,
            "sourceUri": self.source_uri,
            "name": self.name,
            "singleFile": self.single_file,
            "files": {path: base64.b64encode(data).decode() for path, data in self.files.items()},
        }
        if self.nonce is not None:
            wire["nonce"] = self.nonce
        return wire

    @classmethod
    def from_json(cls, payload: Any) -> PluginCopy | None:
        if not isinstance(payload, Mapping):
            return None
        plugin_id, source, name = payload.get("id"), payload.get("sourceUri"), payload.get("name")
        files = payload.get("files")
        if not all(isinstance(v, str) for v in (plugin_id, source, name)) or not isinstance(
            files, Mapping
        ):
            return None
        nonce = payload.get("nonce")
        try:
            decoded = {str(p): base64.b64decode(str(d), validate=True) for p, d in files.items()}
        except ValueError:
            return None
        return cls(
            plugin_id=str(plugin_id),
            source_uri=str(source),
            name=str(name),
            nonce=nonce if isinstance(nonce, str) else None,
            files=decoded,
            single_file=payload.get("singleFile") is True,
        )

    def entries(self, path: str) -> list[dict[str, Any]] | None:
        """`resourceList` of *path* inside the copy, or ``None`` if not a directory."""
        prefix = f"{path.strip('/')}/" if path.strip("/") else ""
        names: dict[str, str] = {}
        for file in self.files:
            if not file.startswith(prefix):
                continue
            head, _, rest = file[len(prefix) :].partition("/")
            names.setdefault(head, "directory" if rest else "file")
        if not names and prefix:
            return None
        return [{"name": name, "type": kind} for name, kind in sorted(names.items())]


def copy_root(automation: str, plugin_id: str) -> str:
    """The host URI a copy is served under -- per automation, per plugin id."""
    owner = hashlib.sha256(automation.encode()).hexdigest()[:16]
    return f"{COPY_SCHEME}/{owner}/{quote(plugin_id, safe='')}"


def split_copy_uri(uri: str) -> tuple[str, str, str] | None:
    """`(owner hash, plugin id, relative path)` of a copy URI, or ``None``."""
    if not uri.startswith(f"{COPY_SCHEME}/"):
        return None
    parts = uri[len(COPY_SCHEME) + 1 :].split("/", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], unquote(parts[1]), parts[2] if len(parts) == 3 else ""


ListDirectory = Callable[[str], Awaitable[Sequence[Any]]]
ReadFile = Callable[[str], Awaitable[bytes]]


async def capture_plugin(
    entry: Mapping[str, Any], list_directory: ListDirectory, read_file: ReadFile
) -> PluginCopy:
    """Copy the plugin a `ClientPluginCustomization` names, bounded.

    *list_directory* and *read_file* reach the client (`resourceList` /
    `resourceRead`). A plugin whose `uri` will not list is read as one file --
    the same reading `Host._plugin_children` gives a live client's plugin.
    """
    plugin_id, uri, name = entry.get("id"), entry.get("uri"), entry.get("name")
    if not isinstance(plugin_id, str) or not isinstance(uri, str) or not isinstance(name, str):
        raise CaptureError("a customization needs a string id, uri and name")
    nonce = entry.get("nonce")
    copy = PluginCopy(
        plugin_id=plugin_id,
        source_uri=uri,
        name=name,
        nonce=nonce if isinstance(nonce, str) else None,
    )
    budget = [MAX_BYTES]

    async def read(path: str, source: str) -> None:
        if len(copy.files) >= MAX_FILES:
            raise CaptureError(f"{name!r} has more than {MAX_FILES} files")
        try:
            data = await read_file(source)
        except Exception as exc:
            raise CaptureError(f"could not read {source}: {exc}") from exc
        budget[0] -= len(data)
        if budget[0] < 0:
            raise CaptureError(f"{name!r} is larger than {MAX_BYTES} bytes")
        copy.files[path] = data

    try:
        top = await list_directory(uri)
    except Exception:
        copy.single_file = True
        await read(uri.rstrip("/").rsplit("/", 1)[-1] or "plugin", uri)
        return copy

    async def walk(entries: Sequence[Any], relative: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise CaptureError(f"{name!r} is nested more than {MAX_DEPTH} levels deep")
        for item in entries:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                continue
            child = item["name"]
            if child in ("", ".", "..") or "/" in child:
                # A name that would escape the copy, or collide with a path.
                raise CaptureError(f"{name!r} lists an invalid entry name {child!r}")
            path = f"{relative}/{child}" if relative else child
            source = f"{uri.rstrip('/')}/{path}"
            if item.get("type") == "directory":
                try:
                    nested = await list_directory(source)
                except Exception as exc:
                    raise CaptureError(f"could not list {source}: {exc}") from exc
                await walk(nested, path, depth + 1)
            else:
                await read(path, source)

    await walk(top, "", 1)
    return copy
