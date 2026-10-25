"""A kept worktree is a changeset on its worker chat, with Apply and Discard (plan §4.4)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from ahp_host_flow import FlowProvider
from ahp_host_flow.worktree import (
    APPLY_OPERATION,
    COMMIT_OPERATION,
    DISCARD_OPERATION,
    worktree_path,
)
from tests.flow_fakes import reset_fakes
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import notifications, run_turn, state, worker_chats

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH"),
]

TOML = """
[providers.rec]
load = "tests.flow_fakes:RecordingProvider"

[providers.fw]
load = "tests.flow_fakes:FileWriterProvider"

[routing]
default = "rec"
rules = [
  { provider = "fw", label = "fw*" },
]
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    reset_fakes()
    yield
    reset_fakes()


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=flow-test", "-c", "user.email=flow-test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def init_repo(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    git(ws, "init", "-q")
    (ws / "README.md").write_text("hello", encoding="utf-8")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "init")


async def boot(tmp_path: Path, node_path: str) -> FlowFixture:
    init_repo(tmp_path / "ws")
    return await boot_flow(tmp_path, node_path, TOML)


def script(prompt: str, label: str = "fw-1") -> str:
    return (
        f"```js\nreturn await agent({prompt!r}, {{label: {label!r}, isolation: 'worktree'}})\n```"
    )


def run_id_of(turn: object) -> str:
    (line,) = [n for n in notifications(turn) if n.startswith("Run ")]  # type: ignore[arg-type]
    return line.removeprefix("Run ")


def changeset_uris(fx: FlowFixture, session: str) -> list[str]:
    (worker,) = worker_chats(fx.host, session)
    chat = state(fx.host, str(worker["resource"]))
    return [str(entry["uriTemplate"]) for entry in chat.get("changesets", [])]


async def run_and_find(fx: FlowFixture, tmp_path: Path) -> tuple[dict[str, Any], str, str]:
    turn = await run_turn(fx.wire, fx.chat, "t1", script("write notes.txt"))
    run_id = run_id_of(turn)
    (uri,) = changeset_uris(fx, SESSION)
    return turn, run_id, uri


async def test_flow_changesets_apply(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        _, run_id, uri = await run_and_find(fx, tmp_path)
        channel = state(fx.host, uri)
        (file,) = channel["files"]
        assert file["edit"]["after"]["uri"] == (fx.workspace / "notes.txt").as_uri()
        assert file["edit"]["diff"] == {"added": 1, "removed": 0}
        assert [op["id"] for op in channel["operations"]] == [
            APPLY_OPERATION,
            COMMIT_OPERATION,
            DISCARD_OPERATION,
        ]
        reply = await fx.wire.request(
            "invokeChangesetOperation", {"channel": uri, "operationId": APPLY_OPERATION}
        )
        assert "error" not in reply
        assert (fx.workspace / "notes.txt").read_text(encoding="utf-8") == "x"
        assert not worktree_path(tmp_path / "state", run_id, 0).exists()
        assert state(fx.host, uri)["files"] == []
    finally:
        await fx.shut()


async def test_flow_changesets_discard(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        _, run_id, uri = await run_and_find(fx, tmp_path)
        reply = await fx.wire.request(
            "invokeChangesetOperation", {"channel": uri, "operationId": DISCARD_OPERATION}
        )
        assert "error" not in reply
        assert not (fx.workspace / "notes.txt").exists()
        assert not worktree_path(tmp_path / "state", run_id, 0).exists()
        assert state(fx.host, uri)["files"] == []
    finally:
        await fx.shut()


async def test_flow_changesets_none_when_unchanged(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", script("nothing", "fw-2"))
        assert changeset_uris(fx, SESSION) == []
        assert not worktree_path(tmp_path / "state", run_id_of(turn), 0).exists()
    finally:
        await fx.shut()


async def test_flow_changesets_second_resolve_fails(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        _, _, uri = await run_and_find(fx, tmp_path)
        params = {"channel": uri, "operationId": APPLY_OPERATION}
        assert "error" not in await fx.wire.request("invokeChangesetOperation", params)
        again = await fx.wire.request("invokeChangesetOperation", params)
        # Measured: once resolved the changeset is republished without operations,
        # so the host refuses a repeat at its declared-operations check, before
        # the handler runs.  The handler's own guard is reached by a direct call.
        assert "does not declare" in again["error"]["message"]
        changesets = cast(FlowProvider, fx.host.providers["flow"]).worktree_changesets
        with pytest.raises(RuntimeError, match="no worktree is kept"):
            await changesets.apply(uri)
        with pytest.raises(RuntimeError, match="no worktree is kept"):
            await changesets.discard(uri)
    finally:
        await fx.shut()


async def test_flow_changesets_nothing_to_review_keeps_and_says_how_to_remove(
    tmp_path: Path, node_path: str
) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", script("commit-empty"))
        path = worktree_path(tmp_path / "state", run_id_of(turn), 0)
        # An empty commit moves HEAD but leaves the tree, so the worktree is "changed" with
        # no file to show: kept (a commit must not be lost), no changeset, and a line that
        # says why and how to remove it.
        assert path.exists()
        assert changeset_uris(fx, SESSION) == []
        (line,) = [n for n in notifications(turn) if "worktree kept at" in n]
        assert line.startswith("agent() #0: worktree kept at ")
        assert ", but no reviewable changes; remove with `git worktree remove --force " in line
        assert str(path) in line
        assert "its changeset" not in line
    finally:
        await fx.shut()


RETRY_TOML = (
    '[providers.fw]\nload = "tests.flow_fakes:FileWriterProvider"\nretry_empty = 1\n'
    '\n[routing]\ndefault = "fw"\n'
)


async def test_flow_changesets_retry_gets_its_own_worktree(tmp_path: Path, node_path: str) -> None:
    """A retried attempt runs in a worktree of its own, `<n>-a<k>`; each is kept."""
    init_repo(tmp_path / "ws")
    fx = await boot_flow(tmp_path, node_path, RETRY_TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", script("write-empty-first notes.txt"))
        run_id = run_id_of(turn)
        first = worktree_path(tmp_path / "state", run_id, 0)
        second = worktree_path(tmp_path / "state", run_id, 0, attempt=1)
        assert second == first.with_name("0-a1")
        assert final_json(turn) == str(second)
        assert (first / "notes.txt").exists()
        assert (second / "notes.txt").exists()
        chats = worker_chats(fx.host, SESSION)
        assert len(chats) == 2
        for chat in chats:
            assert len(state(fx.host, str(chat["resource"])).get("changesets", [])) == 1
    finally:
        await fx.shut()
