"""Owner-only files, on the platform the test runs on.

`assert_owner_only` is what `test_store.py` and `test_wirelog.py` assert with:
the POSIX mode bits, or on Windows the DACL -- because `os.chmod` "succeeds"
there while only toggling the read-only attribute, and asserting
`st_mode & 0o077 == 0` on Windows tests nothing the platform enforces.

The Windows half is in CI's gating Windows job (`.github/workflows/ci.yml`).
"""

from __future__ import annotations

import logging
import re
import stat
import sys
from pathlib import Path

import pytest

from ahp_host.core import private_files
from ahp_host.core.private_files import make_owner_only, owner_only_dacl

_ACE = re.compile(r"\(([^;()]*);([^;()]*);([^;()]*);[^;()]*;[^;()]*;([^;()]*)\)")


def _aces(sddl: str) -> list[tuple[str, str, str, str]]:
    """`(type, flags, rights, trustee)` for every ACE in a DACL's SDDL."""
    assert sddl.startswith("D:"), sddl
    return [(m[1], m[2], m[3], m[4]) for m in _ACE.finditer(sddl)]


def assert_owner_only(path: Path, *, directory: bool) -> None:
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if directory else 0o600)
        return
    sddl = private_files.windows_dacl(path)
    # As Windows spells it in SDDL: the SID, or an alias such as `LA` when the
    # account is a well-known one (CI's runner is the built-in Administrator).
    user = private_files.windows_user_trustee()
    # Exactly one ACE, allowing the current user; no Users, Authenticated
    # Users, Everyone, Administrators or SYSTEM entry inherited from above.
    assert [(kind, trustee) for kind, _flags, _rights, trustee in _aces(sddl)] == [("A", user)], (
        sddl
    )


class TestPortable:
    def test_a_file_is_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_text("x")
        assert make_owner_only(path, directory=False)
        assert_owner_only(path, directory=False)

    def test_a_directory_is_owner_only(self, tmp_path: Path) -> None:
        directory = tmp_path / "d"
        directory.mkdir(mode=0o755)
        assert make_owner_only(directory, directory=True)
        assert_owner_only(directory, directory=True)

    def test_a_missing_path_is_logged_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="ahp_host.core.private_files"):
            assert not make_owner_only(tmp_path / "absent", directory=False)
        assert "owner-only" in caplog.text

    def test_the_windows_dacl_is_protected_with_one_ace(self) -> None:
        """`P`: nothing inherited from the parent. `OICI` only on a directory,
        so what is created inside it inherits the single ACE."""
        assert owner_only_dacl("S-1-5-21-1-2-3-1001", directory=True) == (
            "D:P(A;OICI;FA;;;S-1-5-21-1-2-3-1001)"
        )
        assert owner_only_dacl("S-1-5-21-1-2-3-1001", directory=False) == (
            "D:P(A;;FA;;;S-1-5-21-1-2-3-1001)"
        )


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ACLs need Windows")
class TestWindows:
    def test_the_directory_acl_is_protected(self, tmp_path: Path) -> None:
        directory = tmp_path / "d"
        directory.mkdir()
        make_owner_only(directory, directory=True)
        sddl = private_files.windows_dacl(directory)
        assert sddl.startswith("D:P"), sddl
        assert [flags for _kind, flags, _rights, _trustee in _aces(sddl)] == ["OICI"], sddl

    def test_a_file_created_afterwards_inherits_it(self, tmp_path: Path) -> None:
        directory = tmp_path / "d"
        directory.mkdir()
        make_owner_only(directory, directory=True)
        (directory / "new.txt").write_text("x")
        assert_owner_only(directory / "new.txt", directory=False)

    def test_the_trustee_is_the_user_as_windows_writes_it(self) -> None:
        trustee = private_files.windows_user_trustee()
        sid = private_files.windows_user_sid()
        # A real account's SID, or the alias of a well-known one -- never empty.
        assert trustee == sid or (trustee.isalpha() and len(trustee) == 2)

    def test_a_file_that_already_existed_is_covered_too(self, tmp_path: Path) -> None:
        """No traverse check on Windows (everyone holds
        `SeChangeNotifyPrivilege`), so a locked directory does not hide a
        readable file inside it the way `0o700` does on POSIX. The ACL is
        re-propagated to existing children instead."""
        directory = tmp_path / "d"
        directory.mkdir()
        (directory / "old.txt").write_text("x")
        make_owner_only(directory, directory=True)
        assert_owner_only(directory / "old.txt", directory=False)
