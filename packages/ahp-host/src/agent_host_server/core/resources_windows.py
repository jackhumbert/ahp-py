"""The filesystem jail on Windows: read-only, handle-relative, no string re-parsing.

The POSIX jail in :mod:`agent_host_server.core.resources` walks with
`openat(dir_fd, name, O_NOFOLLOW)`. Windows' Python has neither `dir_fd` nor
`O_NOFOLLOW`, and the obvious substitutes are each the classic escape:

* `os.path.realpath`, compare, then `open(path)` re-parses the path string at
  open time, so a component swapped for a junction after the check is followed.
* `CreateFileW` on a whole path string also re-parses every intermediate
  component, and Win32 path parsing *normalises* before it resolves: trailing
  dots and spaces are stripped, `/` becomes `\\`, `CON`/`NUL`/`COM1` become
  devices anywhere in a path, `file.txt:stream` names an alternate data stream,
  and `\\\\?\\`, UNC and 8.3 short names are all alternative spellings. A jail
  that compares strings has to reproduce every one of those rules exactly.

So this module does neither. The technique, and why it is safe:

1. **Each component is opened relative to the handle of its parent** with
   `NtCreateFile`, passing the parent in `OBJECT_ATTRIBUTES.RootDirectory` and
   a single path component as the name. That is the NT-native `openat`: the
   kernel looks the one name up inside the already-open directory, and there is
   no path string for anybody to re-interpret. Names go to the filesystem
   verbatim -- none of the Win32 normalisation above runs at this layer -- so a
   device name, a stream or a trailing dot is not *reinterpreted* here; it is
   refused up front by :func:`check_component` anyway, both as defence in depth
   and because such a name has no unambiguous Win32 spelling to publish.
2. **`FILE_OPEN_REPARSE_POINT`** on every open, which is `O_NOFOLLOW` for the
   final (and only) component: a symlink, junction, mount point or any other
   reparse point is opened *as itself*, never traversed by the kernel. Its tag
   is then read off the handle (`FileAttributeTagInfo`). A name-surrogate
   (a link of any kind) is either resolved HERE -- the target read with
   `FSCTL_GET_REPARSE_POINT`, mapped to a root-relative path, and re-walked from
   the root exactly like the POSIX jail re-walks a symlink -- or refused.
3. **Share mode `FILE_SHARE_READ | FILE_SHARE_WRITE`, deliberately without
   `FILE_SHARE_DELETE`.** While a component's handle is held, nobody can rename
   or delete it, so the directory the next lookup happens in cannot be swapped
   out from under the walk.
4. **Containment is verified on the opened handle**, not on the string asked
   for: `GetFinalPathNameByHandleW` of every child must be exactly its parent's
   final path plus one component. The resolved, long (never 8.3), stored-case
   name from that answer is what the canonical URI is built from, so the
   policy sees the file that will actually be read.
5. **The root is re-opened and identity-checked on every walk** (volume serial
   plus file id). If the served directory is renamed, or replaced by a junction,
   the walk refuses instead of starting somewhere else.

The string comparisons that remain -- mapping a peer's URI or a link target onto
the root, case-insensitively and drive-aware -- only decide *which root-relative
components to walk*. They never decide containment: every walk starts at the
root handle and descends one verified child at a time, so a comparison that is
too lenient can at worst name a different file *inside* the root, and one that
is too strict refuses. Both fail closed.

Read-only. `writable=True` raises at construction: the write half needs
create/rename/delete through handles with the same guarantees, and a jail that
is safe only for reads should not be one flag away from being unsafe for writes.

The pure parts (URI mapping, component validation, reparse-buffer parsing, link
target mapping) are ordinary functions at module level and are tested on every
platform. The Win32 calls are confined to :class:`_Win32`, which is only
constructed on Windows.
"""

from __future__ import annotations

import ctypes
import importlib
import mimetypes
import os
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse

from agent_host_protocol import errors

from agent_host_server.core.resources import (
    _MAX_SYMLINKS,
    DirectoryEntry,
    ResourceContent,
    ResourceInfo,
    RootedFilesystemResourceProvider,
    _iso,
)

__all__ = [
    "WindowsRootedFilesystemResourceProvider",
    "check_component",
    "link_replacement",
    "parse_reparse_buffer",
    "relative_to_root",
    "split_dos_path",
    "split_file_uri",
    "strict_ancestor_depth",
    "uri_from_parts",
]

# ─── pure: names, paths, URIs ─────────────────────────────────────────────

#: Win32 device names. Reserved in ANY directory and with ANY extension
#: (`nul.txt` is still NUL on older Windows), and matched case-insensitively.
#: The superscript digits are real: Windows treats `COM¹` as a device.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "0123456789¹²³"}
)

#: Characters Win32 refuses in a name, plus both separators. `:` is the one
#: that matters most: `file.txt:secret` and `file.txt::$DATA` name alternate
#: data streams, which are a second, unlisted body behind a listed name.
_FORBIDDEN_CHARACTERS = frozenset('<>:"/\\|?*')

_DRIVE_PATH = re.compile(r"([A-Za-z]):(?:[\\/](.*))?", re.DOTALL)


def _denied(detail: str) -> errors.AhpError:
    return errors.AhpError(-32009, f"Not permitted: {detail}")


def fold(text: str) -> str:
    """Case-fold the way NTFS compares names, closely enough to map paths.

    NTFS upcases each UTF-16 unit through the volume's `$UpCase` table; that is
    a one-to-one simple mapping, so `ß` stays `ß` rather than becoming `SS` as
    `str.upper()` would make it. Exactness is not load-bearing -- see the module
    docstring: this decides which components to walk, never containment.
    """
    return "".join(c if len(c.upper()) != 1 else c.upper() for c in text)


def check_component(name: str) -> None:
    """Refuse one path component that Windows would reinterpret. `PermissionDenied`.

    `..` is refused outright, as on POSIX. The rest are the names Win32
    normalises into something else -- a trailing dot or space is stripped, so
    `secret.txt.` IS `secret.txt`; a device name is a device in every directory;
    `:` opens a stream -- and a jail that let them through would be granting
    access by a spelling the policy never saw.
    """
    if name == "..":
        raise _denied("`..` is not permitted in a resource path")
    if name in ("", "."):
        raise _denied("empty path component")
    if any(ord(c) < 32 or c in _FORBIDDEN_CHARACTERS for c in name):
        raise _denied(f"reserved character in {name!r}")
    if name.endswith((".", " ")):
        raise _denied(f"trailing dot or space in {name!r}")
    if fold(name.split(".", 1)[0].rstrip(" ")) in _RESERVED_NAMES:
        raise _denied(f"{name!r} is a device name")


def _components(text: str) -> list[str]:
    """Split on both separators, dropping empty and `.` components."""
    return [part for part in re.split(r"[\\/]", text) if part not in ("", ".")]


def split_file_uri(uri: str) -> tuple[str, list[str]]:
    """A Windows `file:` URI as `(drive, components)`, or `InvalidParams`.

    Accepts `file:///C:/Users/me` and VS Code's `file:///c%3A/Users/me`, and
    `localhost` as the authority. Refuses every other authority and
    `file:////server/share` (UNC: another machine's filesystem, which this
    provider does not mediate), a path with no drive letter, a drive-relative
    `C:foo`, and `\\\\?\\` / device-namespace spellings. Components are split on
    `\\` as well as `/`, because after unquoting `%5C` Windows would.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise errors.invalid_params(f"not a file: URI: {uri}")
    if parsed.netloc not in ("", "localhost"):
        raise errors.invalid_params(f"remote file URIs are not supported: {uri}")
    path = unquote(parsed.path)
    if "\x00" in path:
        raise errors.invalid_params(f"NUL in resource URI: {uri}")
    match = _DRIVE_PATH.fullmatch(path[1:]) if path.startswith("/") else None
    if match is None:
        raise errors.invalid_params(f"resource URI must be an absolute path with a drive: {uri}")
    return f"{match[1].upper()}:", _components(match[2] or "")


def split_dos_path(path: str) -> tuple[str, list[str]]:
    """`C:\\a\\b` or `\\\\?\\C:\\a\\b` as `("C:", ["a", "b"])`. Else `ValueError`.

    Used for what `GetFinalPathNameByHandleW` answers and for the body of a link
    target. Everything that is not a drive-letter path -- UNC
    (`\\\\?\\UNC\\server\\share`), a volume GUID (`\\\\?\\Volume{...}`), a bare
    device -- is refused: those name a namespace the drive-rooted root does not.
    """
    body = path[4:] if path.startswith("\\\\?\\") else path
    match = _DRIVE_PATH.fullmatch(body)
    if match is None:
        raise ValueError(f"not a drive-letter path: {path!r}")
    parts = [part for part in (match[2] or "").split("\\") if part != ""]
    if any(part in (".", "..") for part in parts):
        raise ValueError(f"not a normalised path: {path!r}")
    return f"{match[1].upper()}:", parts


def relative_to_root(
    drive: str, parts: Sequence[str], root_drive: str, root_parts: Sequence[str]
) -> list[str] | None:
    """`parts` below the root, case-insensitively and per drive. Else ``None``.

    Compared component by component, never as a string prefix: `C:\\proj2` is
    not under `C:\\proj`.
    """
    if fold(drive) != fold(root_drive) or len(parts) < len(root_parts):
        return None
    if any(fold(a) != fold(b) for a, b in zip(parts, root_parts, strict=False)):
        return None
    return list(parts[len(root_parts) :])


def strict_ancestor_depth(
    drive: str, parts: Sequence[str], root_drive: str, root_parts: Sequence[str]
) -> int | None:
    """How many root components `parts` spells, if it is a STRICT ancestor."""
    if len(parts) >= len(root_parts):
        return None
    if relative_to_root(drive, parts, root_drive, root_parts[: len(parts)]) != []:
        return None
    return len(parts)


def uri_from_parts(drive: str, parts: Sequence[str]) -> str:
    """`file:///C:/Users/me/proj` -- the form VS Code and `Path.as_uri` produce."""
    return PureWindowsPath(f"{drive}\\", *parts).as_uri()


# ─── pure: reparse points ─────────────────────────────────────────────────

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # junctions and volume mount points
IO_REPARSE_TAG_SYMLINK = 0xA000000C
_SYMLINK_FLAG_RELATIVE = 0x1


def is_name_surrogate(tag: int) -> bool:
    """`IsReparseTagNameSurrogate`: the reparse point stands for ANOTHER name.

    Symlinks and junctions, but also WSL symlinks and future link kinds. A
    non-surrogate (OneDrive placeholders, dedup, WOF compression) is the file
    itself with a filter attached, not a redirection.
    """
    return bool(tag & 0x20000000)


def parse_reparse_buffer(data: bytes) -> tuple[int, str, bool]:
    """`REPARSE_DATA_BUFFER` as `(tag, substitute name, is_relative)`.

    Symlink and mount-point layouts only; anything else, or any offset that
    points outside the buffer, is `ValueError` -- the buffer is attacker-shaped.
    """
    if len(data) < 16:
        raise ValueError("reparse buffer too short")
    tag = struct.unpack_from("<I", data, 0)[0]
    if tag == IO_REPARSE_TAG_SYMLINK:
        if len(data) < 20:
            raise ValueError("symlink reparse buffer too short")
        offset, length, _p_off, _p_len, flags = struct.unpack_from("<HHHHI", data, 8)
        base = 20
    elif tag == IO_REPARSE_TAG_MOUNT_POINT:
        offset, length, _p_off, _p_len = struct.unpack_from("<HHHH", data, 8)
        flags = 0
        base = 16
    else:
        raise ValueError(f"unsupported reparse tag {tag:#x}")
    start = base + offset
    if length % 2 or start + length > len(data):
        raise ValueError("reparse substitute name is out of bounds")
    name = data[start : start + length].decode("utf-16-le")
    return tag, name, bool(flags & _SYMLINK_FLAG_RELATIVE)


def link_replacement(
    substitute: str,
    relative: bool,
    link_parent: Sequence[str],
    root_drive: str,
    root_parts: Sequence[str],
) -> list[str]:
    """One link target, as root-relative components to re-walk. Or `PermissionDenied`.

    Mirrors the POSIX `_relink`: `..` inside a RELATIVE target is legitimate and
    is collapsed, and collapsing past the root is the escape. An absolute target
    must be an NT `\\??\\X:\\...` path under the root; UNC targets (which would
    also make the host authenticate to a remote server), volume GUIDs, device
    paths and drive-relative targets are refused rather than interpreted.
    """
    if relative:
        if substitute.startswith(("\\", "/")) or re.match(r"[A-Za-z]:", substitute):
            raise _denied("a rooted or drive-relative link target")
        replacement = [*link_parent, *_components(substitute)]
    else:
        if not substitute.startswith("\\??\\"):
            raise _denied("a link target outside the drive namespace")
        try:
            drive, parts = split_dos_path(substitute[4:])
        except ValueError:
            raise _denied("a link target that is not a local drive path") from None
        below = relative_to_root(drive, parts, root_drive, root_parts)
        if below is None:
            raise _denied("Symbolic link leaves the resource root")
        replacement = below

    collapsed: list[str] = []
    for part in replacement:
        if part == "..":
            if not collapsed:
                raise _denied("Symbolic link leaves the resource root")
            collapsed.pop()
        elif part not in ("", "."):
            check_component(part)
            collapsed.append(part)
    return collapsed


# ─── Win32, via ctypes ───────────────────────────────────────────────────

_FILE_READ_DATA = 0x0001  # FILE_LIST_DIRECTORY on a directory
_FILE_READ_ATTRIBUTES = 0x0080
_SYNCHRONIZE = 0x00100000
_ACCESS = _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
#: No FILE_SHARE_DELETE: a held component cannot be renamed, deleted or replaced.
_SHARE = 0x1 | 0x2
_FILE_OPEN = 1
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_REPARSE_POINT = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x00000040
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_MAXIMUM_REPARSE_DATA_BUFFER_SIZE = 16 * 1024
_FILE_TYPE_DISK = 1

_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_FULL_DIRECTORY_INFO = 14
_FILE_FULL_DIRECTORY_RESTART_INFO = 15
_FILE_ID_INFO = 18

_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5
_ERROR_NO_MORE_FILES = 18
_ERROR_SHARING_VIOLATION = 32
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_MORE_DATA = 234

#: Between 1601-01-01 (FILETIME) and 1970-01-01, in 100 ns ticks.
_FILETIME_EPOCH = 116444736000000000


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_void_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", ctypes.c_uint32),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    # A union of NTSTATUS and PVOID: pointer-sized either way.
    _fields_ = [
        ("Status", ctypes.c_void_p),
        ("Information", ctypes.c_size_t),
    ]


@dataclass(frozen=True)
class _Stat:
    attributes: int
    reparse_tag: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse_point(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_link(stats: _Stat) -> bool:
    return stats.is_reparse_point and is_name_surrogate(stats.reparse_tag)


def _map_winerror(code: int) -> errors.AhpError:
    if code in (_ERROR_ACCESS_DENIED, _ERROR_SHARING_VIOLATION):
        return errors.AhpError(-32009, "Not permitted to access that resource")
    return errors.AhpError(-32008, "No such resource")


class _Win32:
    """Every Win32/NT call the jail makes, with explicit prototypes.

    `restype`/`argtypes` are set on a PRIVATE `WinDLL` instance: without them a
    64-bit `HANDLE` is truncated to a C `int`, and mutating `ctypes.windll`'s
    shared function objects would change them for every other caller in the
    process. `ctypes.WinDLL` and `msvcrt` are looked up dynamically so this
    module imports and type-checks on every platform.
    """

    def __init__(self) -> None:
        factory = getattr(ctypes, "WinDLL", None)
        if factory is None:
            raise OSError("the Windows filesystem jail needs Win32 (ctypes.WinDLL)")
        self._last_error: Any = ctypes.get_last_error  # type: ignore[attr-defined,unused-ignore]
        self._msvcrt: Any = importlib.import_module("msvcrt")
        k: Any = factory("kernel32", use_last_error=True)
        nt: Any = factory("ntdll")
        handle, dword, pvoid = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p

        k.CreateFileW.argtypes = [
            ctypes.c_wchar_p, dword, dword, pvoid, dword, dword, handle,
        ]  # fmt: skip
        k.CreateFileW.restype = handle
        k.CloseHandle.argtypes = [handle]
        k.CloseHandle.restype = ctypes.c_int
        k.GetFinalPathNameByHandleW.argtypes = [handle, ctypes.c_wchar_p, dword, dword]
        k.GetFinalPathNameByHandleW.restype = dword
        k.GetFileInformationByHandle.argtypes = [handle, pvoid]
        k.GetFileInformationByHandle.restype = ctypes.c_int
        k.GetFileInformationByHandleEx.argtypes = [handle, ctypes.c_int, pvoid, dword]
        k.GetFileInformationByHandleEx.restype = ctypes.c_int
        k.GetFileType.argtypes = [handle]
        k.GetFileType.restype = dword
        k.DeviceIoControl.argtypes = [
            handle, dword, pvoid, dword, pvoid, dword, ctypes.POINTER(dword), pvoid,
        ]  # fmt: skip
        k.DeviceIoControl.restype = ctypes.c_int
        nt.NtCreateFile.argtypes = [
            ctypes.POINTER(handle), dword, ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock), pvoid, dword, dword, dword, dword, pvoid, dword,
        ]  # fmt: skip
        nt.NtCreateFile.restype = ctypes.c_int32
        nt.RtlNtStatusToDosError.argtypes = [ctypes.c_int32]
        nt.RtlNtStatusToDosError.restype = dword
        self._k = k
        self._nt = nt
        self._invalid = ctypes.c_void_p(-1).value

    def _fail(self) -> errors.AhpError:
        return _map_winerror(int(self._last_error()))

    def close(self, handle: int) -> None:
        self._k.CloseHandle(handle)

    def open_path(self, path: str) -> int:
        """The ROOT only: the operator named it, so its string is trusted.

        `FILE_FLAG_OPEN_REPARSE_POINT` so a root later replaced by a junction is
        opened as the junction -- whose identity then fails to match.
        """
        result = self._k.CreateFileW(
            path,
            _ACCESS,
            _SHARE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if result is None or result == self._invalid:
            raise self._fail()
        return int(result)

    def open_relative(self, parent: int, name: str) -> int:
        """`openat(parent, name, O_NOFOLLOW)`, in NT terms. See the module docstring."""
        encoded = name.encode("utf-16-le")
        if not encoded or len(encoded) > 0xFFFC:
            raise errors.AhpError(-32008, "No such resource")
        buffer = ctypes.create_string_buffer(encoded + b"\0\0")
        object_name = _UnicodeString(len(encoded), len(encoded) + 2, ctypes.addressof(buffer))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            parent,
            ctypes.pointer(object_name),
            _OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        status_block = _IoStatusBlock()
        result = ctypes.c_void_p()
        status = int(
            self._nt.NtCreateFile(
                ctypes.byref(result),
                _ACCESS,
                ctypes.byref(attributes),
                ctypes.byref(status_block),
                None,
                0,
                _SHARE,
                _FILE_OPEN,
                _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT,
                None,
                0,
            )
        )
        if status != 0:
            # Only STATUS_SUCCESS is accepted. Any other success code (a
            # STATUS_REPARSE that somehow escaped FILE_OPEN_REPARSE_POINT
            # included) is refused rather than reasoned about.
            if status > 0 and result.value:
                self.close(result.value)
            raise _map_winerror(int(self._nt.RtlNtStatusToDosError(status)))
        if not result.value:
            raise errors.AhpError(-32008, "No such resource")
        return int(result.value)

    def final_path(self, handle: int) -> str:
        """`GetFinalPathNameByHandleW(FILE_NAME_NORMALIZED | VOLUME_NAME_DOS)`.

        Normalised means long names in their stored case -- never an 8.3 alias.
        """
        size = 512
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            written = int(self._k.GetFinalPathNameByHandleW(handle, buffer, size, 0))
            if written == 0:
                raise self._fail()
            if written < size:
                return buffer.value
            size = written + 1

    def stat(self, handle: int) -> _Stat:
        info = ctypes.create_string_buffer(52)  # BY_HANDLE_FILE_INFORMATION
        if not self._k.GetFileInformationByHandle(handle, info):
            raise self._fail()
        (attributes, created, _accessed, written, _serial, size_high, size_low) = (
            struct.unpack_from("<IQQQIII", info.raw, 0)
        )
        tag = ctypes.create_string_buffer(8)  # FILE_ATTRIBUTE_TAG_INFO
        if not self._k.GetFileInformationByHandleEx(handle, _FILE_ATTRIBUTE_TAG_INFO, tag, 8):
            raise self._fail()
        tag_attributes, reparse_tag = struct.unpack_from("<II", tag.raw, 0)
        return _Stat(
            attributes=attributes | tag_attributes,
            reparse_tag=reparse_tag if tag_attributes & _FILE_ATTRIBUTE_REPARSE_POINT else 0,
            size=(size_high << 32) | size_low,
            mtime_ns=(written - _FILETIME_EPOCH) * 100,
            ctime_ns=(created - _FILETIME_EPOCH) * 100,
        )

    def identity(self, handle: int) -> bytes:
        """Volume serial plus file id. 128-bit where the filesystem has it (ReFS)."""
        buffer = ctypes.create_string_buffer(24)  # FILE_ID_INFO
        if self._k.GetFileInformationByHandleEx(handle, _FILE_ID_INFO, buffer, 24):
            return buffer.raw
        info = ctypes.create_string_buffer(52)
        if not self._k.GetFileInformationByHandle(handle, info):
            raise self._fail()
        serial, _hi_size, _lo_size, _links, index_high, index_low = struct.unpack_from(
            "<IIIIII", info.raw, 28
        )
        return struct.pack("<IQ", serial, (index_high << 32) | index_low)

    def file_type(self, handle: int) -> int:
        return int(self._k.GetFileType(handle))

    def reparse_data(self, handle: int) -> bytes:
        buffer = ctypes.create_string_buffer(_MAXIMUM_REPARSE_DATA_BUFFER_SIZE)
        returned = ctypes.c_uint32(0)
        ok = self._k.DeviceIoControl(
            handle,
            _FSCTL_GET_REPARSE_POINT,
            None,
            0,
            buffer,
            _MAXIMUM_REPARSE_DATA_BUFFER_SIZE,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            raise self._fail()
        return buffer.raw[: returned.value]

    def list_names(self, handle: int) -> list[tuple[str, int]]:
        """`(name, attributes)` for each entry, enumerated through the handle.

        `FindFirstFileW` takes a path string, which would reopen the directory
        by name; `GetFileInformationByHandleEx(FileFullDirectoryInfo)` reads the
        directory the walk actually holds.
        """
        entries: list[tuple[str, int]] = []
        size = 64 * 1024
        buffer = ctypes.create_string_buffer(size)
        info_class = _FILE_FULL_DIRECTORY_RESTART_INFO
        while True:
            if not self._k.GetFileInformationByHandleEx(handle, info_class, buffer, size):
                code = int(self._last_error())
                if code in (_ERROR_NO_MORE_FILES, _ERROR_FILE_NOT_FOUND):
                    return entries
                if code in (_ERROR_MORE_DATA, _ERROR_INSUFFICIENT_BUFFER) and size < 1 << 22:
                    size *= 4
                    buffer = ctypes.create_string_buffer(size)
                    continue
                raise _map_winerror(code)
            info_class = _FILE_FULL_DIRECTORY_INFO
            raw = buffer.raw
            offset = 0
            while True:
                # FILE_FULL_DIR_INFO: NextEntryOffset, FileIndex, four times,
                # EndOfFile, AllocationSize, FileAttributes, FileNameLength,
                # EaSize, then FileName at byte 68.
                following, attributes, name_length = (
                    struct.unpack_from("<I", raw, offset)[0],
                    *struct.unpack_from("<II", raw, offset + 56),
                )
                start = offset + 68
                try:
                    name = raw[start : start + name_length].decode("utf-16-le")
                except UnicodeDecodeError:
                    # An unpaired surrogate: no URI can name it. Not listed.
                    name = ""
                if name:
                    entries.append((name, attributes))
                if following == 0:
                    break
                offset += following

    def read_all(self, handle: int) -> bytes:
        """Read through the handle the walk verified, never by name.

        `open_osfhandle` TAKES OWNERSHIP of the handle, and this closes it if
        that fails: either way the caller must not close it again. A double
        `CloseHandle` is not harmless -- the number may already belong to
        somebody else's file or socket.
        """
        try:
            descriptor = self._msvcrt.open_osfhandle(
                handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
        except BaseException:
            # Ownership did not transfer, so it is still ours to close.
            self.close(handle)
            raise
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read()


# ─── the provider ────────────────────────────────────────────────────────


class WindowsRootedFilesystemResourceProvider(RootedFilesystemResourceProvider):
    """The Windows implementation behind `RootedFilesystemResourceProvider`.

    Constructed automatically on `win32`; do not name it directly. Every public
    method is overridden -- nothing from the POSIX walk runs here -- and the
    subclass relationship exists only so `isinstance` and type annotations keep
    working for embedders.
    """

    def __init__(self, root: Path, *, follow_symlinks: bool = True, writable: bool = False) -> None:
        if writable:
            # Checked FIRST, before anything touches Win32, so the refusal is the
            # same on every platform and cannot be skipped by an import error.
            raise ValueError(
                "RootedFilesystemResourceProvider(writable=True) is not supported on "
                "Windows: the Windows jail is read-only. Serve the directory without "
                "writable=True."
            )
        self._w = _Win32()
        self._follow = follow_symlinks
        self.writable = False
        self._locks = {}

        candidate = os.path.realpath(os.fspath(root))
        if not os.path.isdir(candidate):
            raise ValueError(f"resource root is not a directory: {candidate}")
        if candidate.startswith("\\\\") and not candidate.startswith("\\\\?\\"):
            raise ValueError(f"network (UNC) resource roots are not supported: {candidate}")
        opened = candidate if candidate.startswith("\\\\?\\") else f"\\\\?\\{candidate}"
        handle = self._w.open_path(opened)
        try:
            try:
                drive, parts = split_dos_path(self._w.final_path(handle))
            except ValueError:
                raise ValueError(
                    f"resource root must be on a local drive letter, not a network share, "
                    f"volume GUID or device path: {candidate}"
                ) from None
            stats = self._w.stat(handle)
            if not stats.is_dir or is_name_surrogate(stats.reparse_tag):
                raise ValueError(f"resource root is not a directory: {candidate}")
            self._root_identity = self._w.identity(handle)
        finally:
            self._w.close(handle)
        self._root_drive = drive
        self._root_parts = parts
        self._root_open_path = "\\\\?\\" + str(PureWindowsPath(f"{drive}\\", *parts))
        self.root = Path(PureWindowsPath(f"{drive}\\", *parts))

    # ─── the jail ────────────────────────────────────────────────────────

    def _win_relative(self, uri: str) -> list[str]:
        """The requested path's components below the root, or a refusal."""
        drive, parts = split_file_uri(uri)
        if ".." in parts:
            raise errors.AhpError(-32009, f"Not permitted to access {uri}")
        below = relative_to_root(drive, parts, self._root_drive, self._root_parts)
        if below is None:
            raise errors.AhpError(-32009, f"Not permitted to access {uri}")
        for part in below:
            check_component(part)
        return below

    def _win_open_root(self) -> tuple[int, str]:
        handle = self._w.open_path(self._root_open_path)
        try:
            if self._w.identity(handle) != self._root_identity:
                raise errors.AhpError(-32009, "The resource root has been replaced")
            return handle, self._w.final_path(handle).rstrip("\\")
        except BaseException:
            self._w.close(handle)
            raise

    def _win_open_child(self, parent: int, parent_final: str, part: str) -> tuple[int, str, str]:
        """Open one component below `parent` and prove it IS below `parent`.

        Returns the handle, its final path, and its stored (long, true-case)
        name. The caller owns the handle.
        """
        check_component(part)
        child = self._w.open_relative(parent, part)
        try:
            final = self._w.final_path(child).rstrip("\\")
            head, _sep, name = final.rpartition("\\")
            if head != parent_final or not name:
                raise errors.AhpError(-32009, "Resource resolved outside its parent")
            check_component(name)
            return child, final, name
        except BaseException:
            self._w.close(child)
            raise

    def _win_walk(self, parts: Sequence[str], hops: int = 0) -> tuple[int, _Stat, list[str]]:
        """Open the target without ever letting the kernel follow a reparse point.

        Returns an open handle, its stat, and the canonical components below the
        root. The caller closes the handle.
        """
        if hops > _MAX_SYMLINKS:
            raise errors.AhpError(-32009, "Too many symbolic links")
        handle, final = self._win_open_root()
        resolved: list[str] = []
        try:
            stats = self._w.stat(handle)
            for index, part in enumerate(parts):
                if not stats.is_dir:
                    raise errors.AhpError(-32008, "No such resource")
                child, child_final, name = self._win_open_child(handle, final, part)
                try:
                    child_stats = self._w.stat(child)
                    replacement = (
                        self._win_follow(child, child_stats, resolved)
                        if _is_link(child_stats)
                        else None
                    )
                except BaseException:
                    self._w.close(child)
                    raise
                if replacement is not None:
                    # Resolved HERE and re-walked from the root, exactly as the
                    # POSIX jail does: a link never buys an unchecked traversal.
                    self._w.close(child)
                    self._w.close(handle)
                    handle = -1
                    return self._win_walk([*replacement, *parts[index + 1 :]], hops + 1)
                self._w.close(handle)
                handle, final, stats = child, child_final, child_stats
                resolved.append(name)
            return handle, stats, resolved
        except BaseException:
            if handle != -1:
                self._w.close(handle)
            raise

    def _win_follow(self, link: int, stats: _Stat, link_parent: list[str]) -> list[str]:
        """A link's target as root-relative components, or `PermissionDenied`."""
        if not self._follow:
            raise errors.AhpError(-32009, "Not following a link: this provider does not")
        if stats.reparse_tag not in (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT):
            # WSL symlinks, and any link kind that does not exist yet.
            raise errors.AhpError(-32009, f"Unsupported link type {stats.reparse_tag:#x}")
        try:
            _tag, substitute, relative = parse_reparse_buffer(self._w.reparse_data(link))
        except ValueError:
            raise errors.AhpError(-32009, "Unreadable link") from None
        return link_replacement(
            substitute, relative, link_parent, self._root_drive, self._root_parts
        )

    def _win_uri(self, resolved: Sequence[str]) -> str:
        return uri_from_parts(self._root_drive, [*self._root_parts, *resolved])

    def _win_info(self, resolved: Sequence[str], stats: _Stat) -> ResourceInfo:
        directory = stats.is_dir
        name = resolved[-1] if resolved else ""
        return ResourceInfo(
            uri=self._win_uri(resolved),
            type="directory" if directory else "file",
            size=None if directory else stats.size,
            mtime=_iso(stats.mtime_ns / 1e9),
            ctime=_iso(stats.ctime_ns / 1e9),
            content_type=None if directory else mimetypes.guess_type(name)[0],
            # The same shape as POSIX, and nanosecond-grained for the same
            # reason: FILETIME has 100 ns resolution.
            etag=None if directory else f'W/"{stats.size}-{stats.mtime_ns}"',
        )

    def _win_ancestor(self, uri: str) -> int | None:
        """How many root components `uri` names, if a STRICT ancestor. See POSIX."""
        try:
            drive, parts = split_file_uri(uri)
        except errors.AhpError:
            return None
        if ".." in parts:
            return None
        return strict_ancestor_depth(drive, parts, self._root_drive, self._root_parts)

    # ─── the provider surface ────────────────────────────────────────────

    async def resolve(self, uri: str, *, follow_symlinks: bool = True) -> ResourceInfo:
        depth = self._win_ancestor(uri)
        if depth is not None:
            # Bare, and spelled the way the root spells it: see
            # `RootedFilesystemResourceProvider._strict_ancestor`.
            return ResourceInfo(uri=self._win_uri_above(depth), type="directory")
        parts = self._win_relative(uri)
        if not follow_symlinks and parts:
            info = self._win_lstat(parts)
            if info is not None:
                return info
        handle, stats, resolved = self._win_walk(parts)
        try:
            return self._win_info(resolved, stats)
        finally:
            self._w.close(handle)

    def _win_uri_above(self, depth: int) -> str:
        return uri_from_parts(self._root_drive, self._root_parts[:depth])

    def _win_lstat(self, parts: list[str]) -> ResourceInfo | None:
        """lstat semantics: describe a final link without traversing it."""
        parent, _stats, resolved = self._win_walk(parts[:-1])
        try:
            parent_final = self._w.final_path(parent).rstrip("\\")
            link, _final, name = self._win_open_child(parent, parent_final, parts[-1])
        finally:
            self._w.close(parent)
        try:
            stats = self._w.stat(link)
        finally:
            self._w.close(link)
        if not _is_link(stats):
            return None
        return ResourceInfo(
            uri=self._win_uri([*resolved, name]),
            type="symlink",
            size=stats.size,
            mtime=_iso(stats.mtime_ns / 1e9),
        )

    async def read(self, uri: str) -> ResourceContent:
        handle, stats, resolved = self._win_walk(self._win_relative(uri))
        owned = True
        try:
            if stats.is_dir:
                raise errors.invalid_params(f"{uri} is a directory")
            if stats.is_reparse_point:
                # A non-surrogate reparse point: a cloud placeholder (OneDrive
                # Files On-Demand), a deduplicated or WOF-compressed file. The
                # handle was opened WITHOUT reparse processing, so its data is
                # the raw on-disk stream, not what the filter would serve.
                # Re-opening with processing enabled means a second lookup by
                # name, which is the window this module exists to close -- so
                # these are refused, not read.
                raise errors.AhpError(
                    -32009, "Not reading a file whose content is provided by a filesystem filter"
                )
            if self._w.file_type(handle) != _FILE_TYPE_DISK:
                raise errors.AhpError(-32009, "Not a regular file")
            owned = False
            data = self._w.read_all(handle)
        finally:
            if owned:
                self._w.close(handle)
        return ResourceContent(
            data=data, content_type=mimetypes.guess_type(resolved[-1] if resolved else "")[0]
        )

    async def list_dir(self, uri: str) -> Sequence[DirectoryEntry]:
        depth = self._win_ancestor(uri)
        if depth is not None:
            return [DirectoryEntry(name=self._root_parts[depth], type="directory")]
        handle, stats, _resolved = self._win_walk(self._win_relative(uri))
        try:
            if not stats.is_dir:
                raise errors.invalid_params(f"{uri} is not a directory")
            names = self._w.list_names(handle)
        finally:
            self._w.close(handle)
        entries: list[DirectoryEntry] = []
        for name, attributes in sorted(names):
            if name in (".", ".."):
                continue
            try:
                check_component(name)
            except errors.AhpError:
                # A name only the NT layer can create (`foo.`, `CON`): no URI
                # this jail accepts can address it, so it is not offered.
                continue
            # A junction or directory symlink carries the directory attribute
            # itself, so a link is typed by what it claims to be without the
            # target ever being touched -- unlike a stat, which would follow it
            # out of the root to find out.
            entries.append(
                DirectoryEntry(
                    name=name,
                    type="directory" if attributes & _FILE_ATTRIBUTE_DIRECTORY else "file",
                )
            )
        return entries

    # ─── the write half: refused ─────────────────────────────────────────

    def _read_only(self, uri: str) -> errors.AhpError:
        return errors.AhpError(-32009, f"This host is read-only: {uri}")

    async def write(
        self,
        uri: str,
        data: bytes,
        *,
        mode: str = "truncate",
        position: int = 0,
        create_only: bool = False,
        if_match: str | None = None,
    ) -> None:
        raise self._read_only(uri)

    async def mkdir(self, uri: str) -> None:
        raise self._read_only(uri)

    async def delete(self, uri: str, *, recursive: bool = False) -> None:
        raise self._read_only(uri)

    async def move(self, source: str, destination: str, *, fail_if_exists: bool = False) -> None:
        raise self._read_only(destination)

    async def copy(self, source: str, destination: str, *, fail_if_exists: bool = False) -> None:
        raise self._read_only(destination)
