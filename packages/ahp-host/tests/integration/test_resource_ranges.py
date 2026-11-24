"""Ranged `resourceRead` (`position` / `length`, upstream 1.2.0).

"A receiver that honors the range SHOULD return only the requested bytes and
MUST include `size`". This host honours it on every resource it serves:
files through a provider that reads ranges, files through one that does not,
and content it holds itself. The cases are the spec's own rules, plus the one
the range exists for: a file above the read budget, read in pieces.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.resources import (
    DirectoryEntry,
    RangedResourceProvider,
    ResourceContent,
    ResourceInfo,
    RootedFilesystemResourceProvider,
)
from ahp_host.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    served = tmp_path.resolve() / "ws"
    served.mkdir()
    (served / "a.txt").write_text("hello, world")
    (served / "snow.txt").write_bytes("a☃b".encode())  # the snowman is 3 bytes
    (served / "blob.bin").write_bytes(bytes(range(256)))
    return served


class _WholeOnly:
    """A provider with no `read_range`: the host reads whole and slices."""

    def __init__(self, inner: RootedFilesystemResourceProvider) -> None:
        self._inner = inner

    async def resolve(self, uri: str, *, follow_symlinks: bool = True) -> ResourceInfo:
        return await self._inner.resolve(uri, follow_symlinks=follow_symlinks)

    async def read(self, uri: str) -> ResourceContent:
        return await self._inner.read(uri)

    async def list_dir(self, uri: str) -> Sequence[DirectoryEntry]:
        return await self._inner.list_dir(uri)


async def _connect(host: Host) -> tuple[FakeClient, asyncio.Task[None]]:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    await client.request(
        "initialize",
        {"channel": ROOT_URI, "clientId": "ranges", "protocolVersions": ["1.1.0"]},
    )
    return client, task


@pytest.fixture(params=["ranged", "whole-only"])
async def client(root: Path, request: pytest.FixtureRequest) -> AsyncIterator[FakeClient]:
    jail = RootedFilesystemResourceProvider(root)
    resources: Any = jail if request.param == "ranged" else _WholeOnly(jail)
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), resources=resources)
    connected, task = await _connect(host)
    try:
        yield connected
    finally:
        task.cancel()
        await host.aclose()


async def _read(client: FakeClient, uri: str, **extra: Any) -> dict[str, Any]:
    return await client.request("resourceRead", {"channel": ROOT_URI, "uri": uri, **extra})


def test_the_jail_reads_ranges_itself() -> None:
    assert isinstance(RangedResourceProvider, type)
    assert issubclass(RootedFilesystemResourceProvider, RangedResourceProvider)


class TestTheRange:
    async def test_a_range_returns_only_its_bytes_and_the_size(
        self, client: FakeClient, root: Path
    ) -> None:
        result = (await _read(client, (root / "a.txt").as_uri(), position=7, length=5))["result"]
        assert result == {
            "data": "world",
            "encoding": "utf-8",
            "contentType": "text/plain",
            "size": 12,
        }

    async def test_position_alone_reads_to_the_end(self, client: FakeClient, root: Path) -> None:
        result = (await _read(client, (root / "a.txt").as_uri(), position=7))["result"]
        assert (result["data"], result["size"]) == ("world", 12)

    async def test_length_alone_reads_from_the_start(self, client: FakeClient, root: Path) -> None:
        result = (await _read(client, (root / "a.txt").as_uri(), length=5))["result"]
        assert (result["data"], result["size"]) == ("hello", 12)

    async def test_a_length_past_the_end_is_cut_at_the_end(
        self, client: FakeClient, root: Path
    ) -> None:
        result = (await _read(client, (root / "a.txt").as_uri(), position=10, length=99))["result"]
        assert (result["data"], result["size"]) == ("ld", 12)

    async def test_a_position_at_or_past_the_end_is_empty(
        self, client: FakeClient, root: Path
    ) -> None:
        for position in (12, 500):
            result = (await _read(client, (root / "a.txt").as_uri(), position=position))["result"]
            assert (result["data"], result["size"]) == ("", 12)

    async def test_a_whole_read_carries_no_size(self, client: FakeClient, root: Path) -> None:
        """ "Unless `size` is present, the result contains the whole resource.\""""
        result = (await _read(client, (root / "a.txt").as_uri()))["result"]
        assert result["data"] == "hello, world"
        assert "size" not in result

    async def test_binary_goes_as_base64(self, client: FakeClient, root: Path) -> None:
        result = (await _read(client, (root / "blob.bin").as_uri(), position=250, length=4))[
            "result"
        ]
        assert result["encoding"] == "base64"
        assert base64.b64decode(result["data"]) == bytes([250, 251, 252, 253])
        assert result["size"] == 256

    @pytest.mark.parametrize(
        "extra",
        [
            {"position": -1},
            {"length": -1},
            {"position": 1.5},
            {"length": "4"},
            {"position": True},
        ],
    )
    async def test_a_bad_range_is_invalid_params(
        self, client: FakeClient, root: Path, extra: dict[str, Any]
    ) -> None:
        response = await _read(client, (root / "a.txt").as_uri(), **extra)
        assert response["error"]["code"] == -32602, response

    async def test_a_bad_range_on_a_missing_file_is_still_invalid_params(
        self, client: FakeClient, root: Path
    ) -> None:
        response = await _read(client, (root / "nope.txt").as_uri(), position=-1)
        assert response["error"]["code"] == -32602, response


class TestUtf8:
    """ "A ranged result MUST NOT split a UTF-8 sequence, if returned with
    `utf-8` encoding.\""""

    async def test_a_range_ending_inside_a_sequence_stops_before_it(
        self, client: FakeClient, root: Path
    ) -> None:
        # Bytes 0..2 are "a" and the first two bytes of the snowman.
        result = (await _read(client, (root / "snow.txt").as_uri(), length=3))["result"]
        assert (result["data"], result["encoding"], result["size"]) == ("a", "utf-8", 5)

    async def test_a_range_starting_inside_a_sequence_goes_as_base64(
        self, client: FakeClient, root: Path
    ) -> None:
        result = (await _read(client, (root / "snow.txt").as_uri(), position=2, length=3))["result"]
        assert result["encoding"] == "base64"
        assert base64.b64decode(result["data"]) == "☃b".encode()[1:]

    async def test_whole_sequences_stay_text(self, client: FakeClient, root: Path) -> None:
        result = (await _read(client, (root / "snow.txt").as_uri(), position=1, length=3))["result"]
        assert (result["data"], result["encoding"]) == ("☃", "utf-8")


class TestTheBudget:
    async def test_a_file_above_the_budget_is_read_a_slice_at_a_time(self, root: Path) -> None:
        """The range exists for this: a whole read of the file is refused, and
        through a provider that reads ranges, slices within the budget are not."""
        big = root / "big.bin"
        big.write_bytes(b"x" * (2 * 1024 * 1024) + b"end")
        host = Host(
            EchoProvider(),
            LoopbackSingleUserPolicy(),
            resources=RootedFilesystemResourceProvider(root),
            max_read_bytes=1024 * 1024,
        )
        client, task = await _connect(host)
        try:
            whole = await _read(client, big.as_uri())
            assert whole["error"]["code"] == -32009
            tail = (await _read(client, big.as_uri(), position=2 * 1024 * 1024))["result"]
            assert (tail["data"], tail["size"]) == ("end", 2 * 1024 * 1024 + 3)
            too_big = await _read(client, big.as_uri(), position=0, length=1024 * 1024 + 1)
            assert too_big["error"]["code"] == -32009
        finally:
            task.cancel()
            await host.aclose()

    async def test_without_ranges_in_the_provider_the_whole_file_is_budgeted(
        self, root: Path
    ) -> None:
        """Slicing after a whole read still holds the whole file, so the budget
        is the file's, and the refusal stays."""
        big = root / "big.bin"
        big.write_bytes(b"x" * (2 * 1024 * 1024))
        host = Host(
            EchoProvider(),
            LoopbackSingleUserPolicy(),
            resources=_WholeOnly(RootedFilesystemResourceProvider(root)),
            max_read_bytes=1024 * 1024,
        )
        client, task = await _connect(host)
        try:
            response = await _read(client, big.as_uri(), position=0, length=16)
            assert response["error"]["code"] == -32009
        finally:
            task.cancel()
            await host.aclose()
