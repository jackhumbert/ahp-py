"""The Windows jail's pure half, on every platform.

Everything here is string and byte handling -- URI parsing, component
validation, reparse-buffer parsing, link-target mapping -- so it runs on the
macOS and Linux CI legs too. The parts that need real junctions and real
handles are in `test_resource_jail_windows.py`, which only runs on Windows.

None of these functions decides containment on its own (every walk starts at
the root handle and descends one verified child at a time), but each one is
where a Windows spelling trick would first get in, so each is pinned here.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest
from ahp_protocol.errors import AhpError

from ahp_host.core.resources import RootedFilesystemResourceProvider
from ahp_host.core.resources_windows import (
    IO_REPARSE_TAG_MOUNT_POINT,
    IO_REPARSE_TAG_SYMLINK,
    WindowsRootedFilesystemResourceProvider,
    check_component,
    fold,
    is_name_surrogate,
    link_replacement,
    parse_reparse_buffer,
    relative_to_root,
    split_dos_path,
    split_file_uri,
    strict_ancestor_depth,
    uri_from_parts,
)

ROOT = ("C:", ["Users", "me", "proj"])


def _code(call: object) -> int:
    assert callable(call)
    with pytest.raises(AhpError) as caught:
        call()
    return caught.value.code


class TestFileUris:
    def test_the_plain_form(self) -> None:
        assert split_file_uri("file:///C:/Users/me/proj") == ("C:", ["Users", "me", "proj"])

    def test_vs_codes_encoded_colon_and_a_lowercase_drive(self) -> None:
        """VS Code's `URI.toString()` writes `file:///c%3A/...`."""
        assert split_file_uri("file:///c%3A/Users/me") == ("C:", ["Users", "me"])

    def test_localhost_is_this_machine(self) -> None:
        assert split_file_uri("file://localhost/C:/x") == ("C:", ["x"])

    def test_a_bare_drive(self) -> None:
        assert split_file_uri("file:///C:") == ("C:", [])
        assert split_file_uri("file:///C:/") == ("C:", [])

    def test_an_encoded_backslash_is_a_separator(self) -> None:
        """Windows would treat it as one after unquoting, so the jail does too --
        which means the `..` it hides is seen and refused."""
        assert split_file_uri("file:///C:/a%5Cb") == ("C:", ["a", "b"])
        assert split_file_uri("file:///C:/a%5C..%5Cb") == ("C:", ["a", "..", "b"])

    @pytest.mark.parametrize(
        "uri",
        [
            "file://server/share/x",  # UNC authority
            "file:////server/share/x",  # UNC in the path
            "file:///%5C%5Cserver%5Cshare",  # UNC, encoded
            "file:///%5C%5C%3F%5CC:%5Cx",  # \\?\C:\x
            "file:///%5C%5C.%5CPhysicalDrive0",  # \\.\ device namespace
            "file:///etc/passwd",  # no drive
            "file:///C:foo",  # drive-relative
            "file:///C:/a%00b",  # NUL
            "git-blob:/abc",
            "/etc/passwd",
            "C:\\Users",
        ],
    )
    def test_everything_else_is_invalid_params(self, uri: str) -> None:
        assert _code(lambda: split_file_uri(uri)) == -32602

    def test_uris_come_back_in_the_same_form(self) -> None:
        assert uri_from_parts("C:", ["Users", "me", "proj"]) == "file:///C:/Users/me/proj"
        assert uri_from_parts("C:", []) == "file:///C:/"
        assert uri_from_parts("D:", ["a b", "é.txt"]) == "file:///D:/a%20b/%C3%A9.txt"

    @pytest.mark.parametrize(
        "parts", [["Users", "me"], ["a b", "é", "日本語.txt"], ["x#y", "50%", "q?"]]
    )
    def test_round_trip(self, parts: list[str]) -> None:
        assert split_file_uri(uri_from_parts("C:", parts)) == ("C:", parts)


class TestComponents:
    @pytest.mark.parametrize(
        "name",
        [
            "hello.txt",
            "CONSOLE",
            "console.txt",
            "NULL",
            "COM10",
            ".gitignore",
            "PROGRA~1",
            "a b",
            "日本語",
        ],
    )
    def test_ordinary_names_pass(self, name: str) -> None:
        check_component(name)

    @pytest.mark.parametrize(
        "name",
        [
            "..",
            # alternate data streams
            "file.txt:secret",
            "file.txt::$DATA",
            "dir:$I30:$INDEX_ALLOCATION",
            # device names, any case, any extension, trailing spaces before it
            "CON",
            "con",
            "Nul",
            "NUL.txt",
            "nul .txt",
            "AUX",
            "PRN",
            "COM1",
            "com9.log",
            "LPT1",
            "LPT0",
            "COM¹",
            "CONIN$",
            "conout$",
            # Win32 strips these, so `secret.txt.` IS `secret.txt`
            "secret.txt.",
            "secret.txt ",
            "dir...",
            # separators and wildcards
            "a\\b",
            "a/b",
            "a*",
            "a?",
            "a<b",
            'a"b',
            "a|b",
            "a\x00b",
            "a\x1fb",
        ],
    )
    def test_names_windows_would_reinterpret_are_refused(self, name: str) -> None:
        assert _code(lambda: check_component(name)) == -32009

    def test_fold_is_one_to_one(self) -> None:
        """NTFS upcases unit by unit; `ß` does not become `SS`."""
        assert fold("Straße") == "STRAßE"
        assert fold("proj") == fold("PROJ")


class TestContainmentMapping:
    def test_below_the_root_case_insensitively(self) -> None:
        assert relative_to_root("c:", ["USERS", "Me", "PROJ", "sub"], *ROOT) == ["sub"]

    def test_the_root_itself(self) -> None:
        assert relative_to_root("C:", ["Users", "me", "proj"], *ROOT) == []

    def test_another_drive_is_outside(self) -> None:
        assert relative_to_root("D:", ["Users", "me", "proj", "x"], *ROOT) is None

    def test_a_string_prefix_is_not_containment(self) -> None:
        """`C:\\Users\\me\\proj2` starts with `C:\\Users\\me\\proj`."""
        assert relative_to_root("C:", ["Users", "me", "proj2", "x"], *ROOT) is None

    def test_an_8_3_spelling_of_the_root_is_outside(self) -> None:
        """Fails closed: the short name is not mapped, so it is refused."""
        assert relative_to_root("C:", ["USERS~1", "me", "proj", "x"], *ROOT) is None

    def test_strict_ancestors(self) -> None:
        assert strict_ancestor_depth("C:", [], *ROOT) == 0
        assert strict_ancestor_depth("c:", ["users"], *ROOT) == 1
        assert strict_ancestor_depth("C:", ["Users", "ME"], *ROOT) == 2
        assert strict_ancestor_depth("C:", ["Users", "me", "proj"], *ROOT) is None
        assert strict_ancestor_depth("C:", ["Users", "other"], *ROOT) is None
        assert strict_ancestor_depth("D:", [], *ROOT) is None

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("C:\\Users\\me", ("C:", ["Users", "me"])),
            ("\\\\?\\c:\\Users\\me\\", ("C:", ["Users", "me"])),
            ("\\\\?\\C:\\", ("C:", [])),
        ],
    )
    def test_dos_paths(self, path: str, expected: tuple[str, list[str]]) -> None:
        assert split_dos_path(path) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "\\\\?\\UNC\\server\\share\\x",
            "\\\\server\\share\\x",
            "\\\\?\\Volume{0b1c2d3e-0000-0000-0000-100000000000}\\x",
            "\\Device\\HarddiskVolume1\\x",
            "C:\\a\\..\\b",
            "relative\\path",
        ],
    )
    def test_only_drive_letter_paths_are_dos_paths(self, path: str) -> None:
        with pytest.raises(ValueError, match="path"):
            split_dos_path(path)


def _symlink_buffer(substitute: str, *, relative: bool) -> bytes:
    name = substitute.encode("utf-16-le")
    body = struct.pack("<HHHHI", 0, len(name), len(name), 0, 1 if relative else 0) + name
    return struct.pack("<IHH", IO_REPARSE_TAG_SYMLINK, len(body), 0) + body


def _junction_buffer(substitute: str) -> bytes:
    name = substitute.encode("utf-16-le") + b"\0\0"
    body = struct.pack("<HHHH", 0, len(name) - 2, len(name), 0) + name + b"\0\0"
    return struct.pack("<IHH", IO_REPARSE_TAG_MOUNT_POINT, len(body), 0) + body


class TestReparseBuffers:
    def test_a_relative_symlink(self) -> None:
        tag, name, relative = parse_reparse_buffer(_symlink_buffer("..\\x.txt", relative=True))
        assert (tag, name, relative) == (IO_REPARSE_TAG_SYMLINK, "..\\x.txt", True)

    def test_an_absolute_symlink(self) -> None:
        parsed = parse_reparse_buffer(_symlink_buffer("\\??\\C:\\x", relative=False))
        assert parsed == (IO_REPARSE_TAG_SYMLINK, "\\??\\C:\\x", False)

    def test_a_junction(self) -> None:
        parsed = parse_reparse_buffer(_junction_buffer("\\??\\C:\\Users\\me\\proj\\sub\\"))
        assert parsed == (IO_REPARSE_TAG_MOUNT_POINT, "\\??\\C:\\Users\\me\\proj\\sub\\", False)

    def test_an_out_of_bounds_name_is_refused(self) -> None:
        """The buffer is attacker-shaped: a peer that can make links chooses it."""
        whole = _symlink_buffer("\\??\\C:\\x", relative=False)
        with pytest.raises(ValueError, match="out of bounds"):
            parse_reparse_buffer(whole[:-4])

    def test_an_unknown_tag_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            parse_reparse_buffer(struct.pack("<IHH", 0xA000001D, 8, 0) + b"\0" * 8)

    def test_link_kinds_are_name_surrogates(self) -> None:
        assert is_name_surrogate(IO_REPARSE_TAG_SYMLINK)
        assert is_name_surrogate(IO_REPARSE_TAG_MOUNT_POINT)
        assert is_name_surrogate(0xA000001D)  # WSL symlink: refused, not followed
        assert not is_name_surrogate(0x9000001A)  # a OneDrive placeholder


class TestLinkTargets:
    def _map(self, target: str, *, relative: bool, parent: list[str] | None = None) -> list[str]:
        return link_replacement(target, relative, parent or [], *ROOT)

    def test_an_absolute_target_inside(self) -> None:
        assert self._map("\\??\\C:\\Users\\me\\proj\\sub\\x", relative=False) == ["sub", "x"]

    def test_an_absolute_target_inside_in_another_case(self) -> None:
        assert self._map("\\??\\c:\\users\\ME\\Proj\\sub", relative=False) == ["sub"]

    def test_a_junction_with_a_trailing_backslash(self) -> None:
        assert self._map("\\??\\C:\\Users\\me\\proj\\sub\\", relative=False) == ["sub"]

    def test_a_relative_target_climbing_within_the_root(self) -> None:
        assert self._map("..\\hello.txt", relative=True, parent=["sub"]) == ["hello.txt"]

    @pytest.mark.parametrize(
        ("target", "relative", "parent"),
        [
            ("\\??\\C:\\Users\\me\\secret.txt", False, []),
            ("\\??\\C:\\Users\\me\\proj2\\x", False, []),
            ("\\??\\D:\\Users\\me\\proj\\x", False, []),
            ("\\??\\UNC\\attacker\\share\\x", False, []),  # would also leak NTLM
            ("\\??\\Volume{0b1c2d3e-0000-0000-0000-100000000000}\\", False, []),
            ("\\Device\\HarddiskVolume1\\x", False, []),
            ("\\??\\C:\\Users\\me\\proj\\..\\..\\secret", False, []),
            ("C:\\Users\\me\\proj\\x", False, []),  # not an NT path
            ("..\\..\\..\\outside\\secret.txt", True, ["sub"]),
            ("..\\x", True, []),
            ("\\Windows\\win.ini", True, []),  # rooted on the current drive
            ("C:secret", True, []),  # drive-relative
            ("hello.txt:stream", True, []),
            ("CON", True, []),
            ("hello.txt.", True, []),
        ],
    )
    def test_everything_else_is_refused(
        self, target: str, relative: bool, parent: list[str]
    ) -> None:
        assert _code(lambda: self._map(target, relative=relative, parent=parent)) == -32009


class TestConstruction:
    def test_writable_is_refused_before_anything_else(self, tmp_path: Path) -> None:
        """On every platform, so the refusal cannot be skipped by a missing DLL."""
        with pytest.raises(ValueError, match="read-only"):
            WindowsRootedFilesystemResourceProvider(tmp_path, writable=True)

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX class is selected off Windows")
    def test_posix_platforms_keep_the_posix_class(self, tmp_path: Path) -> None:
        assert type(RootedFilesystemResourceProvider(tmp_path)) is RootedFilesystemResourceProvider

    @pytest.mark.skipif(sys.platform == "win32", reason="Win32 exists there")
    def test_the_windows_class_says_why_it_cannot_run_here(self, tmp_path: Path) -> None:
        with pytest.raises(OSError, match="Win32"):
            WindowsRootedFilesystemResourceProvider(tmp_path)


def test_windows_serves_is_drive_aware_and_case_insensitive() -> None:
    from ahp_host.core.resources_windows import windows_serves

    root = ("D:", ["work"])
    assert windows_serves("file:///D:/work", *root)
    assert windows_serves("file:///d%3A/WORK/proj", *root)
    assert windows_serves("file:///D:/work/a/b", *root)
    assert not windows_serves("file:///D:/work2", *root)
    assert not windows_serves("file:///C:/work", *root)
    assert not windows_serves("file:///D:/", *root)
    assert not windows_serves("file:///D:/work/../secret", *root)
    # Not a file: URI at all - not this jail's to judge.
    assert windows_serves("vscode-agent-host://x/y", *root)


def test_posix_serves_matches_the_hosts_old_rule(tmp_path: Path) -> None:
    from ahp_host.core.resources import RootedFilesystemResourceProvider

    if sys.platform == "win32":
        pytest.skip("POSIX provider")
    provider = RootedFilesystemResourceProvider(tmp_path)
    assert provider.serves(tmp_path.resolve().as_uri())
    assert provider.serves((tmp_path.resolve() / "x").as_uri())
    assert not provider.serves(tmp_path.resolve().parent.as_uri())
    assert provider.serves("echo:/not-a-file")
