"""A kept worktree's changeset survives a host restart; a deleted directory drops it."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from ahp_host_flow.worktree import APPLY_OPERATION, DISCARD_OPERATION, worktree_path
from tests.flow_fakes import reset_fakes
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, restart_flow, store_for
from tests.hosting import run_turn, state, worker_chats
from tests.integration.test_flow_changesets import TOML, git, init_repo, run_id_of, script
from tests.integration.test_flow_changesets import changeset_uris as _changeset_uris

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH"),
]


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    reset_fakes()
    yield
    reset_fakes()


def changeset_uris(fx: FlowFixture) -> list[str]:
    return _changeset_uris(fx, SESSION)


async def run_first(tmp_path: Path, node_path: str) -> tuple[str, str]:
    """Boot on a store, run the worktree script and shut; the run id and the first changeset."""
    init_repo(tmp_path / "ws")
    fx = await boot_flow(tmp_path, node_path, TOML, store=store_for(tmp_path))
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", script("write notes.txt"))
        (uri1,) = changeset_uris(fx)
        return run_id_of(turn), uri1
    finally:
        await fx.shut()


async def test_flow_restart_changesets_apply(tmp_path: Path, node_path: str) -> None:
    run_id, uri1 = await run_first(tmp_path, node_path)
    fx2 = await restart_flow(tmp_path, node_path, TOML)
    try:
        (uri2,) = changeset_uris(fx2)
        assert uri2 != uri1
        assert not fx2.host.sequencer.has_channel(uri1)
        channel = state(fx2.host, uri2)
        assert [op["id"] for op in channel["operations"]] == [APPLY_OPERATION, DISCARD_OPERATION]
        reply = await fx2.wire.request(
            "invokeChangesetOperation", {"channel": uri2, "operationId": APPLY_OPERATION}
        )
        assert "error" not in reply
        assert (tmp_path / "ws" / "notes.txt").read_text(encoding="utf-8") == "x"
        assert not worktree_path(tmp_path / "state", run_id, 0).exists()
    finally:
        await fx2.shut()


async def test_flow_restart_changesets_drops_a_deleted_worktree(
    tmp_path: Path, node_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    run_id, _ = await run_first(tmp_path, node_path)
    ws = tmp_path / "ws"
    shutil.rmtree(worktree_path(tmp_path / "state", run_id, 0))
    git(ws, "worktree", "prune")
    with caplog.at_level("WARNING"):
        fx2 = await restart_flow(tmp_path, node_path, TOML)
    try:
        (worker,) = worker_chats(fx2.host, SESSION)
        assert not state(fx2.host, str(worker["resource"])).get("changesets")
        assert not state(fx2.host, SESSION).get("changesets")
        assert any("dropping kept worktree" in r.getMessage() for r in caplog.records)
    finally:
        await fx2.shut()
