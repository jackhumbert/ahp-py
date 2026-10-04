"""The bounded capture of a client plugin (`core/plugin_copies.py`)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from ahp_host.core import plugin_copies
from ahp_host.core.plugin_copies import CaptureError, PluginCopy, capture_plugin, split_copy_uri

pytestmark = pytest.mark.anyio

_ENTRY = {"id": "p", "uri": "virtual://c/p", "name": "P", "nonce": "1"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _client(tree: dict[str, bytes]) -> tuple[Any, Any]:
    async def list_directory(uri: str) -> Sequence[Any]:
        prefix = f"{uri.rstrip('/')}/"
        names: dict[str, str] = {}
        for key in tree:
            if key.startswith(prefix):
                head, _, rest = key[len(prefix) :].partition("/")
                names[head] = "directory" if rest else "file"
        if not names:
            raise OSError("ENOTDIR")
        return [{"name": n, "type": t} for n, t in names.items()]

    async def read_file(uri: str) -> bytes:
        return tree[uri]

    return list_directory, read_file


async def test_a_tree_is_copied_with_relative_paths() -> None:
    tree = {"virtual://c/p/a.md": b"a", "virtual://c/p/skills/x/SKILL.md": b"x"}
    copy = await capture_plugin(_ENTRY, *_client(tree))
    assert copy.files == {"a.md": b"a", "skills/x/SKILL.md": b"x"}
    assert copy.entries("") == [
        {"name": "a.md", "type": "file"},
        {"name": "skills", "type": "directory"},
    ]
    assert copy.entries("skills/x") == [{"name": "SKILL.md", "type": "file"}]
    assert copy.entries("nope") is None


async def test_a_file_shaped_plugin_is_one_file() -> None:
    entry = {**_ENTRY, "uri": "virtual://c/agent.md"}
    copy = await capture_plugin(entry, *_client({"virtual://c/agent.md": b"hi"}))
    assert copy.single_file
    assert copy.files == {"agent.md": b"hi"}


async def test_too_many_bytes_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_copies, "MAX_BYTES", 3)
    with pytest.raises(CaptureError, match="larger"):
        await capture_plugin(_ENTRY, *_client({"virtual://c/p/a.md": b"four"}))


async def test_too_many_files_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_copies, "MAX_FILES", 1)
    tree = {"virtual://c/p/a.md": b"a", "virtual://c/p/b.md": b"b"}
    with pytest.raises(CaptureError, match="files"):
        await capture_plugin(_ENTRY, *_client(tree))


async def test_a_name_that_would_escape_the_copy_fails() -> None:
    async def list_directory(uri: str) -> Sequence[Any]:
        return [{"name": "..", "type": "directory"}]

    async def read_file(uri: str) -> bytes:
        return b""

    with pytest.raises(CaptureError, match="invalid entry"):
        await capture_plugin(_ENTRY, list_directory, read_file)


async def test_a_copy_round_trips_through_json() -> None:
    copy = PluginCopy("p", "virtual://c/p", "P", "1", {"a.bin": b"\x00\xff"})
    again = PluginCopy.from_json(copy.to_json())
    assert again == copy
    assert again.matches(_ENTRY)
    assert not again.matches({**_ENTRY, "nonce": "2"})


def test_copy_uris_split_back_into_their_parts() -> None:
    root = plugin_copies.copy_root("ahp-automation:/x", "a/b c")
    assert split_copy_uri(f"{root}/dir/f.md") == (root.split("/")[1], "a/b c", "dir/f.md")
    assert split_copy_uri("file:///x") is None
