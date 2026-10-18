"""`native_path_from_file_uri`: what the polling watcher walks.

On Windows `path_from_file_uri("file:///C:/x")` is `/C:/x` (kept verbatim for
the POSIX jail), which names nothing, so the watcher reported no change at all
there. These run on every platform: a URI written by `Path.as_uri` comes back
as a path that exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ahp_protocol import errors

from ahp_host.core.resources import native_path_from_file_uri
from ahp_host.core.watches import PollingResourceWatcher, WatchRequest


def test_a_uri_from_as_uri_round_trips_to_an_existing_path(tmp_path: Path) -> None:
    folder = tmp_path / "a folder"
    folder.mkdir()
    path = native_path_from_file_uri(folder.as_uri())
    assert path == folder
    assert path.is_dir()


@pytest.mark.parametrize("uri", ["virtual:/x", "file://other-host/x"])
def test_the_same_uris_are_refused(uri: str) -> None:
    with pytest.raises(errors.AhpError):
        native_path_from_file_uri(uri)


def test_the_polling_watcher_sees_a_file_under_a_real_root(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    watcher = PollingResourceWatcher()
    seen = watcher._scan(WatchRequest(root=tmp_path.as_uri(), recursive=True))
    assert (tmp_path / "x.txt").as_uri() in seen
