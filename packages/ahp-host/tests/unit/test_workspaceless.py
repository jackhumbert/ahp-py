"""A session with no working directory is marked `_meta.workspaceless` in its summary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI

from ahp_host.core import WORKSPACELESS_META_KEY, Host, LoopbackSingleUserPolicy
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider

from .hosting import Wire, connect, open_session, shut

pytestmark = pytest.mark.anyio

_A, _B = "file:///work/a", "file:///work/b"
_MULTIROOT: dict[str, Any] = {"multipleChats": {}, "multipleWorkingDirectories": {}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _listed(wire: Wire, uri: str) -> dict[str, Any]:
    response = await wire.request("listSessions", {"channel": ROOT_URI})
    (entry,) = [item for item in response["result"]["items"] if item["resource"] == uri]
    return dict(entry)


def _changes(frame: dict[str, Any]) -> dict[str, Any]:
    """The `changes` of a `root/sessionSummaryChanged` frame, else `{}`."""
    if frame.get("method") != "root/sessionSummaryChanged":
        return {}
    return dict(frame["params"]["changes"])


def _host(tmp_path: Path | None = None) -> Host:
    return Host(
        EchoProvider(capabilities=_MULTIROOT),
        LoopbackSingleUserPolicy(),
        store=FileSessionStore(tmp_path) if tmp_path is not None else None,
    )


def test_the_key_is_the_one_clients_read() -> None:
    assert WORKSPACELESS_META_KEY == "workspaceless"


async def test_a_session_without_directories_is_workspaceless() -> None:
    host = _host()
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/w1")
        assert (await _listed(wire, "echo:/w1"))["_meta"] == {WORKSPACELESS_META_KEY: True}
        await open_session(wire, "echo:/w2", workingDirectories=[])
        assert (await _listed(wire, "echo:/w2"))["_meta"][WORKSPACELESS_META_KEY] is True
        added = [
            f["params"]["summary"]
            for f in wire.frames
            if f.get("method") == "root/sessionAdded"
            and f["params"]["summary"]["resource"] == "echo:/w1"
        ]
        assert added[0]["_meta"] == {WORKSPACELESS_META_KEY: True}
    finally:
        await shut(host, wire, serving)


async def test_a_session_with_a_directory_is_not() -> None:
    host = _host()
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/w3", workingDirectories=[_A])
        assert "_meta" not in await _listed(wire, "echo:/w3")
    finally:
        await shut(host, wire, serving)


async def test_adding_a_directory_clears_the_mark() -> None:
    host = _host()
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/w4")
        await wire.dispatch("echo:/w4", {"type": "session/workingDirectorySet", "directory": _B})
        assert await wire.until(
            lambda: bool(host.sequencer.state_of("echo:/w4").get("workingDirectories"))
        )
        assert await wire.until(lambda: any(_changes(f).get("_meta") == {} for f in wire.frames)), (
            "the retraction reaches subscribers of the list"
        )
        assert (await _listed(wire, "echo:/w4"))["_meta"] == {}
    finally:
        await shut(host, wire, serving)


async def test_other_meta_keys_are_kept() -> None:
    host = _host()
    wire, serving = await connect(host)
    try:
        await open_session(wire, "echo:/w5")
        # Server-originated, as a provider would set it: not client-dispatchable.
        await host.sequencer.publish(
            "echo:/w5", {"type": "session/metaChanged", "_meta": {"example.com/k": 1}}
        )
        await host._mirror_summary(host._sessions["echo:/w5"])
        assert await wire.until(
            lambda: any(
                _changes(f).get("_meta") == {"example.com/k": 1, WORKSPACELESS_META_KEY: True}
                for f in wire.frames
            )
        )
        assert (await _listed(wire, "echo:/w5"))["_meta"] == {
            "example.com/k": 1,
            WORKSPACELESS_META_KEY: True,
        }
    finally:
        await shut(host, wire, serving)


async def test_the_mark_survives_a_restart(tmp_path: Path) -> None:
    first = _host(tmp_path)
    wire, serving = await connect(first)
    try:
        await open_session(wire, "echo:/w6")
        await open_session(wire, "echo:/w7", workingDirectories=[_A])
    finally:
        await shut(first, wire, serving)

    second = _host(tmp_path)
    assert await second.restore() == 2
    wire, serving = await connect(second)
    try:
        assert (await _listed(wire, "echo:/w6"))["_meta"] == {WORKSPACELESS_META_KEY: True}
        assert "_meta" not in await _listed(wire, "echo:/w7")
    finally:
        await shut(second, wire, serving)
