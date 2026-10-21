"""A refused working directory falls back to a folder the host actually serves."""

from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.node.roots import NamedRootsResourceProvider, Roots
from ahp_host.provider import EchoProvider

from .hosting import connect, open_session, shut, state

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_named_roots_fall_back_to_the_primary_root_not_the_virtual_top(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    roots = Roots.named({"one": first, "two": second})
    host = Host(
        EchoProvider(),
        LoopbackSingleUserPolicy(),
        resources=NamedRootsResourceProvider(roots),
        default_directory=roots.default_directory(),
    )
    wire, serving = await connect(host)
    try:
        # Refused, so the host falls back (to the primary root, not `file:///`).
        await open_session(wire, "echo:/fb-1", workingDirectories=["file:///nowhere/at/all"])
        assert state(host, "echo:/fb-1")["workingDirectories"] == [roots.primary.as_uri()]
    finally:
        await shut(host, wire, serving)


async def test_a_single_root_host_still_falls_back_to_its_root(tmp_path: Path) -> None:
    from ahp_host.core.resources import RootedFilesystemResourceProvider

    host = Host(
        EchoProvider(),
        LoopbackSingleUserPolicy(),
        resources=RootedFilesystemResourceProvider(tmp_path),
        default_directory=tmp_path.as_uri(),
    )
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/fb-2", workingDirectories=["file:///nowhere/at/all"])
        assert state(host, "echo:/fb-2")["workingDirectories"] == [tmp_path.as_uri()]
    finally:
        await shut(host, wire, serving)
