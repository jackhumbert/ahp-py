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
import binascii
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, unquote, urlparse

from agent_host_protocol.reducers.clock import to_iso
from agent_host_protocol.types import JsonObject

from agent_host_client.client.errors import (
    AlreadyExists,
    Conflict,
    InvalidParams,
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
            _needs(uri, write=False),
        )

    def _require_writable(self, uri: str) -> None:
        if not self._writable:
            raise PermissionDenied(-32009, "this client serves its files read-only", _needs(uri))

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
        uri = _required(params, "uri")
        path = self._contain(uri)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise NotFound(-32008, f"no such file: {uri}") from exc
        except IsADirectoryError as exc:
            raise NotFound(-32008, f"is a directory: {uri}") from exc
        return {**_encode(data, params.get("encoding")), "etag": self._etag(path)}

    def _resource_list(self, params: Mapping[str, Any]) -> JsonObject:
        uri = _required(params, "uri")
        path = self._contain(uri)
        if not path.is_dir():
            raise NotFound(-32008, f"not a directory: {uri}")
        # `DirectoryEntry` is `{name, type}` and nothing else. A `uri` here would
        # be redundant -- the caller joins `name` onto the directory it asked
        # about -- and `kind` is the key nobody reads, so a host keying on `type`
        # per the schema sees every directory as a file and tries to read it.
        entries = [
            {"name": child.name, "type": "directory" if child.is_dir() else "file"}
            for child in sorted(path.iterdir())
        ]
        return {"entries": entries}

    def _resource_resolve(self, params: Mapping[str, Any]) -> JsonObject:
        uri = _required(params, "uri")
        # `followSymlinks: false` is lstat semantics: report the link itself.
        # Containing via the parent rather than `_contain` is what makes that
        # safe -- the link's directory is still jailed, and we never open the
        # target, so a link out of the root discloses nothing but its own name.
        follow = params.get("followSymlinks") is not False
        path = self._contain(uri) if follow else self._contain_new(uri)
        if not path.exists() and not path.is_symlink():
            raise NotFound(-32008, f"no such resource: {uri}")
        stat = path.stat() if follow else path.lstat()
        result: JsonObject = {
            # "Equal to the requested URI when `followSymlinks` is `false`" --
            # and `file_uri` resolves, so passing the link through it would hand
            # back the canonical target under a flag that asked for the link.
            "uri": file_uri(path) if follow else uri,
            "type": _resource_type(path, follow=follow),
            "size": stat.st_size,
            # ISO 8601, not epoch millis: the schema says "Last-modified time in
            # ISO 8601 format", and an integer parses as neither.
            "mtime": to_iso(int(stat.st_mtime * 1000)),
        }
        if follow:
            result["etag"] = self._etag(path)
        return result

    def _resource_request(self, params: Mapping[str, Any]) -> JsonObject:
        """Answer a permission negotiation honestly.

        The result type is "an empty object on success" and a denial "MUST
        respond with `PermissionDenied` (-32009)" -- so a successful
        ``{"granted": false}`` tells the caller asking again would help, which is
        the deny→request→deny loop this method exists to end.

        A request with neither flag set is read, per `ResourceRequestParams`.
        """
        uri = _required(params, "uri")
        self._contain(uri)
        if params.get("write"):
            self._require_writable(uri)
        return {}

    # ── write ────────────────────────────────────────────────────────────────

    def _resource_write(self, params: Mapping[str, Any]) -> JsonObject:
        uri = _required(params, "uri")
        self._require_writable(uri)
        path = self._contain(uri)
        mode = str(params.get("mode") or "truncate")
        if params.get("createOnly") and path.exists():
            raise AlreadyExists(-32010, f"already exists: {uri}")
        if_match = params.get("ifMatch")
        if if_match is not None and path.exists() and self._etag(path) != if_match:
            raise Conflict(-32011, f"etag mismatch for {uri}")

        # `data`, not `content`. Reading the wrong key made a conformant caller's
        # payload vanish and `write_bytes(b"")` run, so a spec-shaped write
        # truncated the file to zero bytes and answered success.
        raw = params.get("data")
        if not isinstance(raw, str):
            raise InvalidParams(-32602, "resourceWrite requires 'data'")
        data = _decode(raw, str(params.get("encoding") or "utf-8"))
        position = int(params.get("position") or 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Byte offsets, not string indices -- the spec is explicit, and any
        # non-ASCII content makes the two disagree. Each mode roots `position`
        # somewhere different, so none of them can share a branch.
        if mode == "append" and position == 0:
            # POSIX append, which MUST be atomic; a seek-then-write race would
            # interleave two writers' bytes.
            with path.open("ab") as handle:
                handle.write(data)
        elif mode == "append":
            existing = path.read_bytes() if path.exists() else b""
            cut = max(0, len(existing) - position)
            path.write_bytes(existing[:cut] + data + existing[cut:])
        elif mode == "insert":
            existing = path.read_bytes() if path.exists() else b""
            path.write_bytes(existing[:position] + data + existing[position:])
        else:
            existing = path.read_bytes() if position and path.exists() else b""
            path.write_bytes(existing[:position] + data)
        return {"etag": self._etag(path)}

    def _resource_mkdir(self, params: Mapping[str, Any]) -> JsonObject:
        uri = _required(params, "uri")
        self._require_writable(uri)
        # Contain via the parent -- the target does not exist yet -- but create
        # the target. Creating `_contain_parent(uri)` itself made `mkdir -p a/b`
        # produce `a` and report success for `a/b`.
        target = self._contain_new(uri)
        if target.exists() and not target.is_dir():
            raise AlreadyExists(-32010, f"exists and is not a directory: {uri}")
        target.mkdir(parents=True, exist_ok=True)
        return {}

    def _resource_delete(self, params: Mapping[str, Any]) -> JsonObject:
        uri = _required(params, "uri")
        self._require_writable(uri)
        path = self._contain(uri)
        if path.is_dir():
            if params.get("recursive"):
                _rmtree(path)
            elif any(path.iterdir()):
                raise Conflict(-32011, f"directory not empty and recursive not set: {uri}")
            else:
                # Only a NON-empty directory needs `recursive`; refusing an empty
                # one made the caller pass a flag that also authorises deleting
                # a tree.
                path.rmdir()
        else:
            path.unlink(missing_ok=True)
        return {}

    def _resource_copy(self, params: Mapping[str, Any]) -> JsonObject:
        return self._transfer(params, move=False)

    def _resource_move(self, params: Mapping[str, Any]) -> JsonObject:
        return self._transfer(params, move=True)

    def _transfer(self, params: Mapping[str, Any], *, move: bool) -> JsonObject:
        source_uri = _required(params, "source")
        destination_uri = _required(params, "destination")
        self._require_writable(destination_uri)
        source = self._contain(source_uri)
        # The destination is contained separately: a copy whose target escapes
        # the root is the same disclosure as a read that does.
        target = self._contain_new(destination_uri)
        if params.get("failIfExists") and target.exists():
            # The caller set this precisely because it did not want the
            # destination's bytes replaced; overwriting anyway loses them and
            # reports success.
            raise AlreadyExists(-32010, f"destination already exists: {destination_uri}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if move:
            os.replace(source, target)
        else:
            target.write_bytes(source.read_bytes())
        return {}

    def _contain_new(self, uri: str) -> Path:
        """Contain a target that need not exist yet, via its nearest parent.

        `_contain` cannot serve this: it resolves and checks the target itself,
        which for a path that does not exist yet says nothing about where it
        would land. Walking up to the nearest *existing* ancestor, resolving
        THAT, and rebuilding the tail underneath it is what makes a `mkdir` or a
        copy destination as jail-safe as a read.
        """
        candidate = path_from_uri(uri)
        parent = candidate.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        resolved_parent = parent.resolve()
        for root in self._roots:
            if resolved_parent == root or root in resolved_parent.parents:
                return resolved_parent / candidate.relative_to(parent)
        raise PermissionDenied(-32009, f"{uri} is outside every served root", _needs(uri))


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
        # Register EVERY ancestor, not just the immediate parent. `put` is the
        # only place that knows the tree, and a host expands a plugin by walking
        # down from its root -- so with only the leaf's parent registered,
        # `plugins/skills/one.md` leaves `plugins` listing empty and the skill is
        # never reached.
        segments = [s for s in uri[len(self._prefix) :].split("/") if s]
        for depth in range(len(segments)):
            parent = _key(self._prefix + "/".join(segments[:depth]))
            child = self._prefix + "/".join(segments[: depth + 1])
            entries = self._children.setdefault(parent, [])
            if child not in entries:
                entries.append(child)
        return uri

    async def handle(self, method: str, params: Mapping[str, Any]) -> JsonObject:
        # Declining comes before validating: -32601 says "I do not do that at
        # all", which is a different instruction to the caller than "your frame
        # was malformed".
        if method not in _VIRTUAL_METHODS:
            raise MethodNotFound(-32601, f"{method} is not served for virtual resources")
        uri = _required(params, "uri")
        if method == "resourceRead":
            data = self._blobs.get(uri)
            if data is None:
                raise NotFound(-32008, f"no such virtual resource: {uri}")
            # Not `errors="replace"`: a published binary is still a file the
            # agent may need byte-exact, and mangling it silently is worse than
            # base64.
            return _encode(data, params.get("encoding"))
        if method == "resourceList":
            children = self._children.get(_key(uri), [])
            return {
                "entries": [
                    {
                        "name": child.rsplit("/", 1)[-1],
                        "type": "file" if child in self._blobs else "directory",
                    }
                    for child in children
                ]
            }
        if method == "resourceResolve":
            if uri in self._blobs:
                return {"uri": uri, "type": "file", "size": len(self._blobs[uri])}
            if _key(uri) in self._children:
                return {"uri": uri, "type": "directory"}
            raise NotFound(-32008, f"no such virtual resource: {uri}")
        # `resourceRequest`: an empty object on success, `PermissionDenied` on
        # refusal. There is no "may create" here -- nothing outside what was
        # published can ever be served -- so an unknown URI is a refusal.
        if uri in self._blobs or _key(uri) in self._children:
            return {}
        raise PermissionDenied(-32009, f"not published by this client: {uri}", _needs(uri))


#: What a `VirtualResourceServer` answers. In-memory content the client minted
#: has no write side: there is nothing behind it to write to.
_VIRTUAL_METHODS: Final[frozenset[str]] = frozenset(
    {"resourceRead", "resourceList", "resourceResolve", "resourceRequest"}
)

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


def _key(uri: str) -> str:
    """Directory keys, trailing slash removed, so lookup and storage agree."""
    return uri.rstrip("/")


def _required(params: Mapping[str, Any], key: str) -> str:
    """A required string param, or `InvalidParams`.

    Coercing the absent case to ``""`` and carrying on reports the mount miss --
    "nothing mounted for ''" -- which tells the caller its URI was fine and the
    resource was gone. -32602 is the code for a frame that was never valid.
    """
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidParams(-32602, f"{key!r} is required")
    return value


def _needs(uri: str, *, write: bool = True) -> JsonObject:
    """`PermissionDeniedErrorData` -- the access that would unlock the call.

    A `ResourceRequestParams`, so it carries the required ``channel``; the
    ``read``/``write`` flag is what a peer renders to the human who can widen
    the mount. The earlier ``{"reason": ...}`` was neither a declared property
    nor a shape any caller could feed back to `resourceRequest`.
    """
    request: JsonObject = {"channel": "ahp-root://", "uri": uri}
    request["write" if write else "read"] = True
    return {"request": request}


def _resource_type(path: Path, *, follow: bool) -> str:
    if not follow and path.is_symlink():
        return "symlink"
    return "directory" if path.is_dir() else "file"


def _encode(data: bytes, preferred: Any) -> JsonObject:
    """`ResourceReadResult` -- ``data`` and ``encoding``, both required.

    "The server SHOULD honor the `encoding` requested in the params. If the
    server cannot provide the requested encoding, it MUST fall back to either
    `base64` or `utf-8`" -- so a request for base64 is answered in base64 even
    for text, and text that is not valid UTF-8 falls back rather than being
    mangled by ``errors="replace"``.
    """
    if preferred != "base64":
        try:
            return {"data": data.decode("utf-8"), "encoding": "utf-8"}
        except UnicodeDecodeError:
            pass
    return {"data": base64.b64encode(data).decode("ascii"), "encoding": "base64"}


def _decode(content: str, encoding: str) -> bytes:
    if encoding != "base64":
        return content.encode("utf-8")
    try:
        return base64.b64decode(content, validate=True)
    except (ValueError, binascii.Error) as exc:
        # Not -32603: the payload was malformed, and a caller told "internal
        # error" retries the same bytes.
        raise InvalidParams(-32602, "data is not valid base64") from exc


def _rmtree(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            _rmtree(child)
        else:
            child.unlink()
    path.rmdir()
