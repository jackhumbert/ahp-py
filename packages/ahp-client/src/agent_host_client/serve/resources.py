"""Serving files and in-memory content back to the host.

Two servers. :class:`FileResourceServer` exposes a real directory, read-only
unless a **second, separate** opt-in says otherwise -- reading discloses and
writing destroys, and the two should not be granted by the same gesture. That
is the sibling host's posture and it is right in this direction too: the peer
asking is an agent.

:class:`VirtualResourceServer` serves bytes the client holds, which is what a
``virtual://`` plugin is: content no filesystem on the host will ever find.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, unquote, urlparse

from agent_host_protocol.types import JsonObject

from agent_host_client.client.errors import (
    AlreadyExists,
    Conflict,
    MethodNotFound,
    NotFound,
    PermissionDenied,
)

__all__ = ["FileResourceServer", "VirtualResourceServer", "file_uri", "path_from_uri"]


def file_uri(path: str | os.PathLike[str]) -> str:
    """RFC 8089 ``file://`` URI for a local path."""
    resolved = Path(path).resolve()
    return "file://" + quote(str(resolved))


def path_from_uri(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise NotFound(-32008, f"not a file URI: {uri!r}")
    return Path(unquote(parsed.path))


class FileResourceServer:
    """A rooted, symlink-safe view of one or more directories.

    **Containment is checked after resolution, not before.** ``realpath`` then
    verify-under-root is the order that survives a symlink swapped between the
    check and the open; checking a pre-resolution path and then opening it is
    the classic TOCTOU shape, and the sibling host has a test for exactly that
    race on its own jail.
    """

    def __init__(
        self,
        *roots: str | os.PathLike[str],
        writable: bool = False,
        follow_symlinks: bool = False,
    ) -> None:
        if not roots:
            raise ValueError("a FileResourceServer with no roots serves nothing")
        self._roots = tuple(Path(r).resolve() for r in roots)
        self._writable = writable
        self._follow_symlinks = follow_symlinks

    # ── the jail ─────────────────────────────────────────────────────────────

    def _contain(self, uri: str) -> Path:
        candidate = path_from_uri(uri)
        resolved = candidate.resolve()
        for root in self._roots:
            if resolved == root or root in resolved.parents:
                if not self._follow_symlinks and candidate != resolved and candidate.is_symlink():
                    raise PermissionDenied(-32009, f"symlink not followed: {uri}")
                return resolved
        raise PermissionDenied(
            -32009,
            f"{uri} is outside every served root",
            {"request": {"uri": uri, "reason": "outside the served roots"}},
        )

    def _require_writable(self, uri: str) -> None:
        if not self._writable:
            raise PermissionDenied(
                -32009,
                "this client serves its files read-only",
                {"request": {"uri": uri, "reason": "write access not granted"}},
            )

    @staticmethod
    def _etag(path: Path) -> str:
        stat = path.stat()
        # ahp-server's format. The spec gives no format and no stability
        # contract, and this one cannot distinguish two same-size writes inside
        # one millisecond -- a real lost-update window for the `ifMatch` flow the
        # etag exists to protect. Logged upstream; matched here for interop.
        return f'W/"{stat.st_size}-{int(stat.st_mtime * 1000)}"'

    # ── dispatch ─────────────────────────────────────────────────────────────

    async def handle(self, method: str, params: Mapping[str, Any]) -> JsonObject:
        handler = getattr(self, f"_{_snake(method)}", None)
        if handler is None:
            raise MethodNotFound(-32601, f"{method} is not served by this resource server")
        result: JsonObject = handler(params)
        return result

    # ── read ─────────────────────────────────────────────────────────────────

    def _resource_read(self, params: Mapping[str, Any]) -> JsonObject:
        path = self._contain(str(params["uri"]))
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise NotFound(-32008, f"no such file: {params['uri']}") from exc
        except IsADirectoryError as exc:
            raise NotFound(-32008, f"is a directory: {params['uri']}") from exc
        try:
            return {"content": data.decode("utf-8"), "etag": self._etag(path)}
        except UnicodeDecodeError:
            # Binary content goes back base64-encoded rather than mangled --
            # `errors="replace"` would hand the agent a file that looks like text
            # and is not the file.
            return {
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
                "etag": self._etag(path),
            }

    def _resource_list(self, params: Mapping[str, Any]) -> JsonObject:
        path = self._contain(str(params["uri"]))
        if not path.is_dir():
            raise NotFound(-32008, f"not a directory: {params['uri']}")
        entries = [
            {
                "uri": file_uri(child),
                "name": child.name,
                "kind": "directory" if child.is_dir() else "file",
            }
            for child in sorted(path.iterdir())
        ]
        return {"entries": entries}

    def _resource_resolve(self, params: Mapping[str, Any]) -> JsonObject:
        path = self._contain(str(params["uri"]))
        if not path.exists():
            raise NotFound(-32008, f"no such resource: {params['uri']}")
        stat = path.stat()
        return {
            "uri": file_uri(path),
            "kind": "directory" if path.is_dir() else "file",
            "size": stat.st_size,
            "mtime": int(stat.st_mtime * 1000),
            "etag": self._etag(path),
        }

    def _resource_request(self, params: Mapping[str, Any]) -> JsonObject:
        """Answer a permission negotiation honestly.

        A client that always says "granted" turns the deny→request→deny loop
        into an infinite one; this reports what is actually true.
        """
        uri = str(params["uri"])
        try:
            self._contain(uri)
        except PermissionDenied:
            return {"granted": False}
        return {"granted": True}

    # ── write ────────────────────────────────────────────────────────────────

    def _resource_write(self, params: Mapping[str, Any]) -> JsonObject:
        uri = str(params["uri"])
        self._require_writable(uri)
        path = self._contain(uri)
        mode = str(params.get("mode") or "truncate")
        if params.get("createOnly") and path.exists():
            raise AlreadyExists(-32010, f"already exists: {uri}")
        if_match = params.get("ifMatch")
        if if_match is not None and path.exists() and self._etag(path) != if_match:
            raise Conflict(-32011, f"etag mismatch for {uri}")

        content = params.get("content")
        data = _decode(content, str(params.get("encoding") or "utf-8"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            # `0` means POSIX append and MUST be atomic; a seek-then-write race
            # would interleave two writers' bytes.
            with path.open("ab") as handle:
                handle.write(data)
        elif mode == "insert":
            position = int(params.get("position") or 0)
            existing = path.read_bytes() if path.exists() else b""
            # Byte offsets, not string indices -- the spec is explicit, and any
            # non-ASCII content makes the two disagree.
            path.write_bytes(existing[:position] + data + existing[position:])
        else:
            path.write_bytes(data)
        return {"etag": self._etag(path)}

    def _resource_mkdir(self, params: Mapping[str, Any]) -> JsonObject:
        uri = str(params["uri"])
        self._require_writable(uri)
        self._contain_parent(uri).mkdir(parents=True, exist_ok=True)
        return {}

    def _resource_delete(self, params: Mapping[str, Any]) -> JsonObject:
        uri = str(params["uri"])
        self._require_writable(uri)
        path = self._contain(uri)
        if path.is_dir():
            if not params.get("recursive"):
                raise Conflict(-32011, f"directory not empty and recursive not set: {uri}")
            _rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return {}

    def _resource_copy(self, params: Mapping[str, Any]) -> JsonObject:
        return self._transfer(params, move=False)

    def _resource_move(self, params: Mapping[str, Any]) -> JsonObject:
        return self._transfer(params, move=True)

    def _transfer(self, params: Mapping[str, Any], *, move: bool) -> JsonObject:
        source_uri = str(params["source"])
        destination_uri = str(params["destination"])
        self._require_writable(destination_uri)
        source = self._contain(source_uri)
        # The destination is contained separately: a copy whose target escapes
        # the root is the same disclosure as a read that does.
        destination = self._contain_parent(destination_uri)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / path_from_uri(destination_uri).name
        if move:
            os.replace(source, target)
        else:
            target.write_bytes(source.read_bytes())
        return {}

    def _contain_parent(self, uri: str) -> Path:
        """Contain a path that does not exist yet, via its nearest parent."""
        candidate = path_from_uri(uri)
        parent = candidate.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        resolved_parent = parent.resolve()
        for root in self._roots:
            if resolved_parent == root or root in resolved_parent.parents:
                return (resolved_parent / candidate.relative_to(parent)).parent
        raise PermissionDenied(-32009, f"{uri} is outside every served root")


class VirtualResourceServer:
    """In-memory content, addressed by URI.

    What a client-published plugin actually is: the host is told a
    ``virtual://`` URI, and the only thing that can serve it is the client that
    minted it.
    """

    def __init__(self, prefix: str = "virtual://") -> None:
        self._prefix = prefix
        self._blobs: dict[str, bytes] = {}
        self._children: dict[str, list[str]] = {}

    @property
    def prefix(self) -> str:
        return self._prefix

    def put(self, path: str, data: bytes | str) -> str:
        uri = path if path.startswith(self._prefix) else f"{self._prefix}{path.lstrip('/')}"
        self._blobs[uri] = data.encode("utf-8") if isinstance(data, str) else data
        parent = uri.rsplit("/", 1)[0]
        if parent != uri:
            self._children.setdefault(parent, []).append(uri)
        return uri

    async def handle(self, method: str, params: Mapping[str, Any]) -> JsonObject:
        uri = str(params.get("uri", ""))
        if method == "resourceRead":
            data = self._blobs.get(uri)
            if data is None:
                raise NotFound(-32008, f"no such virtual resource: {uri}")
            return {"content": data.decode("utf-8", errors="replace")}
        if method == "resourceList":
            children = self._children.get(uri.rstrip("/"), [])
            return {
                "entries": [
                    {"uri": child, "name": child.rsplit("/", 1)[-1], "kind": "file"}
                    for child in children
                ]
            }
        if method == "resourceResolve":
            if uri in self._blobs:
                return {"uri": uri, "kind": "file", "size": len(self._blobs[uri])}
            if uri.rstrip("/") in self._children:
                return {"uri": uri, "kind": "directory"}
            raise NotFound(-32008, f"no such virtual resource: {uri}")
        if method == "resourceRequest":
            return {"granted": uri in self._blobs or uri.rstrip("/") in self._children}
        raise MethodNotFound(-32601, f"{method} is not served for virtual resources")


_SNAKE: Final[dict[str, str]] = {
    "resourceRead": "resource_read",
    "resourceWrite": "resource_write",
    "resourceList": "resource_list",
    "resourceCopy": "resource_copy",
    "resourceDelete": "resource_delete",
    "resourceMove": "resource_move",
    "resourceResolve": "resource_resolve",
    "resourceMkdir": "resource_mkdir",
    "resourceRequest": "resource_request",
}


def _snake(method: str) -> str:
    return _SNAKE.get(method, method)


def _decode(content: Any, encoding: str) -> bytes:
    if content is None:
        return b""
    if isinstance(content, bytes):
        return content
    text = str(content)
    return base64.b64decode(text) if encoding == "base64" else text.encode("utf-8")


def _rmtree(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            _rmtree(child)
        else:
            child.unlink()
    path.rmdir()
