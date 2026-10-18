"""Owner-only files and directories, on POSIX and on Windows.

What the host writes to disk -- stored sessions, automation definitions, the
wire log -- is the conversation in plaintext, so it is made readable by the
user running the host and nobody else.

On POSIX that is a mode: `0o600` for a file, `0o700` for a directory. Asserted
with `chmod` rather than requested at creation, because a creation mode is
masked by the umask and `mkdir(exist_ok=True)` accepts whatever mode it finds.

**On Windows mode bits mean nothing.** `os.chmod` only toggles the read-only
attribute, so it "succeeded" while the file kept whatever ACL it inherited from
its directory. Under a drive root the default ACL hands that down as read
access for `BUILTIN\\Users`, every local account; under a user profile it is
the user, SYSTEM and Administrators, which is safe only by where it was put.
So on Windows the object gets a **protected DACL** (inheritance from the parent
cut off) with a single ACE granting the current user full control, and for a
directory that ACE is inheritable (`OICI`), so every file created inside it
afterwards is owner-only too. It is applied with `SetNamedSecurityInfoW`, which
also re-propagates inheritance to children that already exist: an existing
store directory does not keep a readable file just because the file predates
the call.

What that does NOT exclude, on either platform: the account's own other
processes, and administrators (root on POSIX; on Windows anyone holding the
backup or take-ownership privilege, which includes SYSTEM). Owner-only is the
POSIX `0o600` guarantee restated, not encryption.

Where the filesystem has no permissions at all (FAT/exFAT on Windows, some
network mounts), the call fails and the caller decides: every caller here logs
and carries on, because refusing to start a host over a USB stick's filesystem
helps nobody, and the failure is said out loud rather than swallowed.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path
from typing import Any

__all__ = ["make_owner_only", "owner_only_dacl"]

_log = logging.getLogger(__name__)

# `winnt.h` / `accctrl.h`.
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1


def make_owner_only(path: Path, *, directory: bool) -> bool:
    """Make `path` readable and writable by its owner alone. True if it took.

    Never raises for a filesystem that cannot express it; it logs a warning
    naming the path instead, so an unprotected transcript is never silent.
    """
    try:
        if sys.platform == "win32":
            _set_windows_dacl(path, directory=directory)
        else:
            os.chmod(path, 0o700 if directory else 0o600)
    except OSError as exc:
        _log.warning("could not make %s owner-only: %s", path, exc)
        return False
    return True


def owner_only_dacl(sid: str, *, directory: bool) -> str:
    """The SDDL of the DACL this module applies on Windows.

    `P`: protected, nothing inherited from the parent. One allow ACE, `FA`
    (`FILE_ALL_ACCESS`) for `sid`; `OICI` on a directory so files and
    subdirectories created in it inherit the same single ACE.
    """
    return f"D:P(A;{'OICI' if directory else ''};FA;;;{sid})"


class _Advapi:
    """The few Win32 calls, with prototypes on a private `WinDLL` instance.

    Private for the same reason as the jail's `_Win32`: setting `argtypes` on
    `ctypes.windll`'s shared function objects changes them for every caller in
    the process. Looked up dynamically so this module imports and type-checks
    everywhere.
    """

    def __init__(self) -> None:
        factory = getattr(ctypes, "WinDLL", None)
        if factory is None:
            raise OSError("owner-only ACLs need Win32 (ctypes.WinDLL)")
        self.last_error: Any = ctypes.get_last_error  # type: ignore[attr-defined,unused-ignore]
        self.win_error: Any = getattr(ctypes, "WinError")  # noqa: B009 - Windows-only
        a: Any = factory("advapi32", use_last_error=True)
        k: Any = factory("kernel32", use_last_error=True)
        handle, dword, pvoid, ok = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int
        k.GetCurrentProcess.argtypes = []
        k.GetCurrentProcess.restype = handle
        k.CloseHandle.argtypes = [handle]
        k.CloseHandle.restype = ok
        k.LocalFree.argtypes = [pvoid]
        k.LocalFree.restype = pvoid
        a.OpenProcessToken.argtypes = [handle, dword, ctypes.POINTER(handle)]
        a.OpenProcessToken.restype = ok
        a.GetTokenInformation.argtypes = [handle, ctypes.c_int, pvoid, dword, ctypes.POINTER(dword)]
        a.GetTokenInformation.restype = ok
        a.ConvertSidToStringSidW.argtypes = [pvoid, ctypes.POINTER(ctypes.c_wchar_p)]
        a.ConvertSidToStringSidW.restype = ok
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p, dword, ctypes.POINTER(pvoid), ctypes.POINTER(dword),
        ]  # fmt: skip
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ok
        a.GetSecurityDescriptorDacl.argtypes = [
            pvoid, ctypes.POINTER(ok), ctypes.POINTER(pvoid), ctypes.POINTER(ok),
        ]  # fmt: skip
        a.GetSecurityDescriptorDacl.restype = ok
        a.SetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_int, dword, pvoid, pvoid, pvoid, pvoid,
        ]  # fmt: skip
        a.SetNamedSecurityInfoW.restype = dword
        a.GetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_int, dword,
            pvoid, pvoid, pvoid, pvoid, ctypes.POINTER(pvoid),
        ]  # fmt: skip
        a.GetNamedSecurityInfoW.restype = dword
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            pvoid, dword, dword, ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(dword),
        ]  # fmt: skip
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = ok
        self.a = a
        self.k = k

    def fail(self, code: int | None = None) -> OSError:
        error: OSError = self.win_error(self.last_error() if code is None else code)
        return error

    def user_sid(self) -> str:
        """The string SID of the user this process runs as (`TokenUser`)."""
        token = ctypes.c_void_p()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), _TOKEN_QUERY, token):
            raise self.fail()
        try:
            size = ctypes.c_uint32()
            self.a.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not self.a.GetTokenInformation(
                token, _TOKEN_USER, buffer, size.value, ctypes.byref(size)
            ):
                raise self.fail()
        finally:
            self.k.CloseHandle(token)
        # `TOKEN_USER` starts with `SID_AND_ATTRIBUTES`, whose first field is
        # the `PSID`.
        sid = ctypes.c_void_p.from_buffer(buffer).value
        text = ctypes.c_wchar_p()
        if not self.a.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise self.fail()
        try:
            return str(text.value)
        finally:
            self.k.LocalFree(text)

    def set_dacl(self, path: Path, sddl: str) -> None:
        descriptor = ctypes.c_void_p()
        if not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise self.fail()
        try:
            present, defaulted = ctypes.c_int(), ctypes.c_int()
            dacl = ctypes.c_void_p()
            if not self.a.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            ):
                raise self.fail()
            status = self.a.SetNamedSecurityInfoW(
                str(path),
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
            if status != 0:
                raise self.fail(status)
        finally:
            self.k.LocalFree(descriptor)

    def canonical(self, sddl: str) -> str:
        """`sddl` as Windows writes it back: a well-known SID becomes its alias
        (the built-in Administrator is `LA`, not `S-1-5-21-...-500`)."""
        descriptor = ctypes.c_void_p()
        if not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise self.fail()
        try:
            text = ctypes.c_wchar_p()
            if not self.a.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, _SDDL_REVISION_1, _DACL_SECURITY_INFORMATION, ctypes.byref(text), None
            ):
                raise self.fail()
            try:
                return str(text.value)
            finally:
                self.k.LocalFree(text)
        finally:
            self.k.LocalFree(descriptor)

    def get_dacl(self, path: Path) -> str:
        descriptor = ctypes.c_void_p()
        status = self.a.GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            raise self.fail(status)
        try:
            text = ctypes.c_wchar_p()
            if not self.a.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, _SDDL_REVISION_1, _DACL_SECURITY_INFORMATION, ctypes.byref(text), None
            ):
                raise self.fail()
            try:
                return str(text.value)
            finally:
                self.k.LocalFree(text)
        finally:
            self.k.LocalFree(descriptor)


def _set_windows_dacl(path: Path, *, directory: bool) -> None:
    advapi = _Advapi()
    advapi.set_dacl(path, owner_only_dacl(advapi.user_sid(), directory=directory))


def windows_dacl(path: Path) -> str:
    """The DACL of `path` as SDDL. Windows only; for tests and diagnosis."""
    return _Advapi().get_dacl(path)


def windows_user_sid() -> str:
    """The current user's SID string. Windows only; for tests and diagnosis."""
    return _Advapi().user_sid()


def windows_user_trustee() -> str:
    """The current user as a trustee in the SDDL `windows_dacl` returns: its SID,
    or the alias Windows writes for a well-known one (`LA` for the built-in
    Administrator, which is what CI's Windows runner runs as). Windows only."""
    advapi = _Advapi()
    sddl = advapi.canonical(f"D:(A;;FA;;;{advapi.user_sid()})")
    return sddl.rsplit(";", 1)[1].rstrip(")")
