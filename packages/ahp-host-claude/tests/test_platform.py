"""What has to hold on Windows as well as POSIX."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from agent_host_server_claude.paths import directory_of, local_path_of
from agent_host_server_claude.provider import ClaudeProvider, is_valid_provider_id


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("file:///C:/Users/me/proj", PureWindowsPath("C:/Users/me/proj")),
        ("file:///c:/Users/me/a%20b", PureWindowsPath("c:/Users/me/a b")),
        ("vscode-agent-host://my-pc/C:/Users/me/proj", PureWindowsPath("C:/Users/me/proj")),
        # What a broker forwards once it has stripped its node name.
        ("file:///D:/work", PureWindowsPath("D:/work")),
    ],
)
def test_windows_file_uris_keep_their_drive(uri: str, expected: PureWindowsPath) -> None:
    assert local_path_of(uri, windows=True) == expected


def test_posix_file_uris_are_unchanged() -> None:
    assert local_path_of("file:///Users/me/a%20b", windows=False) == PurePosixPath("/Users/me/a b")
    assert local_path_of("https://example.com/x", windows=False) is None
    assert local_path_of("file://", windows=True) is None


def test_this_os_round_trips_its_own_uris(tmp_path: Path) -> None:
    # `Path.as_uri()` is what the host advertises as the default directory.
    assert directory_of(tmp_path.as_uri()) == tmp_path


@pytest.mark.parametrize("value", ["claude", "claude-mac-mini", "Claude_Laptop2"])
def test_provider_ids(value: str) -> None:
    assert is_valid_provider_id(value)
    assert ClaudeProvider(Path("."), provider_id=value).agent.provider == value


@pytest.mark.parametrize("value", ["", "-x", "has space", "a/b", "x" * 65])
def test_bad_provider_ids_are_refused(value: str) -> None:
    assert not is_valid_provider_id(value)
    with pytest.raises(ValueError, match="invalid provider id"):
        ClaudeProvider(Path("."), provider_id=value)


def test_folder_browsing_is_off_where_the_jail_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from agent_host_server_claude import __main__ as cli

    monkeypatch.setattr(os, "supports_dir_fd", set())
    assert cli._jail_supported() is False


def test_windows_uses_the_servers_windows_jail_when_it_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    from agent_host_server_claude import __main__ as cli

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(
        sys.modules, "agent_host_server.core.resources_windows", types.ModuleType("stub")
    )
    assert cli._jail_supported() is True
    monkeypatch.setitem(sys.modules, "agent_host_server.core.resources_windows", None)
    assert cli._jail_supported() is False
