"""Commit on a kept worktree's changeset, and Discard with a reason (M15 item 4)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from ahp_host.provider import OperationContext
from ahp_protocol.ext.operation import OPERATION_META_KEY

from ahp_host_flow import FlowProvider
from ahp_host_flow import worktree as worktree_module
from ahp_host_flow.provider import FlowSession
from ahp_host_flow.worktree import (
    APPLY_OPERATION,
    COMMIT_OPERATION,
    DISCARD_OPERATION,
    WorktreeChangesets,
    WorktreeError,
    commit_subject,
    create_worktree,
    worktree_path,
)
from tests.flow_fakes import RecordingProvider, reset_fakes
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, restart_flow, store_for
from tests.hosting import notifications, run_turn, start_turn, state, wait_turn, worker_chats
from tests.integration.test_flow_changesets import TOML, changeset_uris, git, init_repo
from tests.integration.test_flowext_hostgates import py
from tests.integration.test_flowprincipal import HeaderPolicy

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH"),
]

WRITE_PY = (
    "meta = {'name': 'writeflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('write notes.txt', label='fw-1', isolation='worktree')\n"
)
NAMED_PY = (
    "meta = {'name': 'namedflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await agent('first', label='rev', x={'chat': 'rev'})\n"
    "    await agent('hold', label='hold')\n"
    "    b = await agent('second', label='rev', x={'chat': 'rev'})\n"
    "    return [a, b]\n"
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    reset_fakes()
    yield
    reset_fakes()


def repo_with_identity(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    init_repo(ws)
    git(ws, "config", "user.name", "flow-test")
    git(ws, "config", "user.email", "flow-test@example.com")
    return ws


async def boot(tmp_path: Path, toml: str = TOML, **boot: Any) -> FlowFixture:
    repo_with_identity(tmp_path)
    return await boot_flow(tmp_path, str(tmp_path / "no-node"), toml, **boot)


async def run_and_find(fx: FlowFixture, tmp_path: Path) -> tuple[str, str]:
    """Run the worktree workflow; the run id and the changeset's URI."""
    name = py(tmp_path, "writeflow", WRITE_PY)
    turn = await run_turn(fx.wire, fx.chat, "t1", name)
    (line,) = [n for n in notifications(turn) if n.startswith("Run ")]
    (uri,) = changeset_uris(fx, SESSION)
    return line.removeprefix("Run "), uri


async def invoke(
    fx: FlowFixture, uri: str, operation: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    request: dict[str, Any] = {"channel": uri, "operationId": operation}
    if params is not None:
        request["_meta"] = {OPERATION_META_KEY: params}
    return await fx.wire.request("invokeChangesetOperation", request)


async def test_flowcommit_creates_the_branch_and_leaves_the_checkout(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        ws = fx.workspace
        head = git(ws, "rev-parse", "HEAD").strip()
        run_id, uri = await run_and_find(fx, tmp_path)
        reply = await invoke(fx, uri, COMMIT_OPERATION, {"message": "Add notes"})
        assert "error" not in reply, reply
        message = reply["result"]["message"]
        assert message.startswith("Committed ")
        assert message.endswith(f"on branch flow/{run_id}-0")
        assert git(ws, "log", "-1", "--format=%B", f"flow/{run_id}-0").strip() == "Add notes"
        assert git(ws, "rev-parse", "HEAD").strip() == head
        assert git(ws, "status", "--porcelain").strip() == ""
        assert not (ws / "notes.txt").exists()
        assert not worktree_path(tmp_path / "state", run_id, 0).exists()
    finally:
        await fx.shut()


async def test_flowcommit_default_message_and_trailer(tmp_path: Path) -> None:
    fx = await boot(
        tmp_path,
        TOML + "\n[changesets]\ntrailer = true\n",
        policy=HeaderPolicy(),
        headers={"x-forwarded-user": "alice"},
    )
    try:
        run_id, uri = await run_and_find(fx, tmp_path)
        reply = await invoke(fx, uri, COMMIT_OPERATION)
        assert "error" not in reply, reply
        body = git(fx.workspace, "log", "-1", "--format=%B", f"flow/{run_id}-0").strip()
        assert body == "fw-1\n\nFlow-Principal: alice"
    finally:
        await fx.shut()
    plain = tmp_path / "plain"
    plain.mkdir()
    fx = await boot(plain)
    try:
        run_id, uri = await run_and_find(fx, plain)
        reply = await invoke(fx, uri, COMMIT_OPERATION)
        assert "error" not in reply, reply
        body = git(fx.workspace, "log", "-1", "--format=%B", f"flow/{run_id}-0").strip()
        assert body == "fw-1"
    finally:
        await fx.shut()


async def test_flowcommit_apply_lands_on_the_working_tree(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        ws = fx.workspace
        head = git(ws, "rev-parse", "HEAD").strip()
        _, uri = await run_and_find(fx, tmp_path)
        reply = await invoke(fx, uri, COMMIT_OPERATION, {"apply": True, "message": "m2"})
        assert "error" not in reply, reply
        assert reply["result"]["message"].endswith("on the working tree")
        assert git(ws, "rev-parse", "HEAD").strip() != head
        assert git(ws, "log", "-1", "--format=%s").strip() == "m2"
        assert (ws / "notes.txt").exists()
        assert git(ws, "status", "--porcelain").strip() == ""
        assert "flow/" not in git(ws, "branch", "--list")
    finally:
        await fx.shut()


async def test_flowcommit_second_resolve_fails(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        _, uri = await run_and_find(fx, tmp_path)
        assert "error" not in await invoke(fx, uri, COMMIT_OPERATION)
        assert "error" in await invoke(fx, uri, COMMIT_OPERATION)
        assert "error" in await invoke(fx, uri, APPLY_OPERATION)
    finally:
        await fx.shut()


async def test_flowcommit_survives_a_restart(tmp_path: Path) -> None:
    repo_with_identity(tmp_path)
    node = str(tmp_path / "no-node")
    fx = await boot_flow(tmp_path, node, TOML, store=store_for(tmp_path))
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", py(tmp_path, "writeflow", WRITE_PY))
        (line,) = [n for n in notifications(turn) if n.startswith("Run ")]
        run_id = line.removeprefix("Run ")
    finally:
        await fx.shut()
    fx2 = await restart_flow(tmp_path, node, TOML)
    try:
        (uri,) = changeset_uris(fx2, SESSION)
        reply = await invoke(fx2, uri, COMMIT_OPERATION, {"message": "after restart"})
        assert "error" not in reply, reply
        subject = git(tmp_path / "ws", "log", "-1", "--format=%s", f"flow/{run_id}-0")
        assert subject.strip() == "after restart"
    finally:
        await fx2.shut()


async def test_flowcommit_discard_reason_notifies_after_a_restart(tmp_path: Path) -> None:
    repo_with_identity(tmp_path)
    node = str(tmp_path / "no-node")
    fx = await boot_flow(tmp_path, node, TOML, store=store_for(tmp_path))
    try:
        await run_turn(fx.wire, fx.chat, "t1", py(tmp_path, "writeflow", WRITE_PY))
        (worker,) = worker_chats(fx.host, SESSION)
        chat = str(worker["resource"])
    finally:
        await fx.shut()
    fx2 = await restart_flow(tmp_path, node, TOML)
    try:
        (uri,) = changeset_uris(fx2, SESSION)
        await fx2.wire.request("subscribe", {"channel": chat})
        before = len(state(fx2.host, chat).get("turns", []))
        reply = await invoke(fx2, uri, DISCARD_OPERATION, {"reason": "not after all"})
        assert "error" not in reply, reply
        assert await fx2.wire.until(lambda: len(state(fx2.host, chat).get("turns", [])) > before)
        turn = state(fx2.host, chat)["turns"][-1]
        assert "Discarded: not after all" in notifications(turn)
    finally:
        await fx2.shut()


async def test_flowcommit_discard_reason_notifies_the_worker_chat(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        _, uri = await run_and_find(fx, tmp_path)
        (worker,) = worker_chats(fx.host, SESSION)
        chat = str(worker["resource"])
        before = len(state(fx.host, chat).get("turns", []))
        reply = await invoke(fx, uri, DISCARD_OPERATION, {"reason": "wrong approach"})
        assert "error" not in reply, reply
        assert await fx.wire.until(lambda: len(state(fx.host, chat).get("turns", [])) > before)
        turn = state(fx.host, chat)["turns"][-1]
        assert "Discarded: wrong approach" in notifications(turn)
        # The changeset's own state has no description; the chat's turn carries it.
        assert "'description': 'Discarded: wrong approach'" in str(state(fx.host, chat))
    finally:
        await fx.shut()


async def test_flowcommit_discard_reason_reaches_a_named_chats_next_turn(
    tmp_path: Path,
) -> None:
    fx = await boot(tmp_path)
    try:
        ws = fx.workspace
        name = py(tmp_path, "namedflow", NAMED_PY)
        await start_turn(fx.wire, fx.chat, "t1", name)
        recorder = RecordingProvider
        assert await fx.wire.until(
            lambda: bool(recorder.instances) and len(recorder.instances[0].sessions) == 2
        )
        sessions = recorder.instances[0].sessions
        provider = fx.host.provider
        assert isinstance(provider, FlowProvider)
        (session,) = [s for s in provider._sessions if isinstance(s, FlowSession)]
        # The "rev" chat is the first worker chat opened.
        rev = str(worker_chats(fx.host, SESSION)[0]["resource"])
        publisher = session.context.publisher
        assert publisher is not None
        wt = await create_worktree(ws, tmp_path / "wt-extra")
        (wt.path / "extra.txt").write_text("y", encoding="utf-8")
        published = await provider.worktree_changesets.publish(
            wt,
            publisher,
            chat=rev,
            title="rev",
            ordinal=0,
            session=SESSION,
            run_id=None,
        )
        assert published is not None
        handlers: Any = provider.changeset_operations()
        await handlers[DISCARD_OPERATION](
            published, DISCARD_OPERATION, None, OperationContext({"reason": "wrong file"}, None)
        )
        sessions[1].released.set()
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert turn is not None
        assert (
            recorder.instances[0]
            .sessions[0]
            .received[-1]
            .text.startswith("[discarded: rev] wrong file\n")
        )
        # The queued line is in the model-facing text only.  The published worker
        # messages (turn.message.text) keep the bare prompt; the fake provider's
        # echoed reply ("rec: ...") legitimately shows what the model received.
        texts = [
            t["message"]["text"]
            for w in worker_chats(fx.host, SESSION)
            for t in state(fx.host, str(w["resource"])).get("turns", [])
        ]
        assert "second" in texts
        assert not any("[discarded:" in t for t in texts)
    finally:
        await fx.shut()


class _Publisher:
    """Just enough of a `SessionPublisher` for `WorktreeChangesets` on its own."""

    def __init__(self) -> None:
        self.n = 0

    async def changes_published(self, changeset: Any, changes: Any, *, chat: str) -> str:
        self.n += 1
        return f"changeset:/{self.n}"


def moved_and_deleted_repo(tmp_path: Path) -> Path:
    """A repository holding move.txt and gone.txt besides README.md."""
    ws = repo_with_identity(tmp_path)
    (ws / "move.txt").write_text("moved content\n" * 20, encoding="utf-8")
    (ws / "gone.txt").write_text("deleted\n", encoding="utf-8")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "two files")
    return ws


async def kept_with_move_and_delete(
    ws: Path, tmp_path: Path, changesets: WorktreeChangesets, **publish: Any
) -> str:
    """A kept worktree that renames move.txt to moved.txt and deletes gone.txt; its URI."""
    wt = await create_worktree(ws, tmp_path / "wt")
    git(wt.path, "mv", "move.txt", "moved.txt")
    (wt.path / "gone.txt").unlink()
    uri = await changesets.publish(
        wt, cast(Any, _Publisher()), chat="chat:/w", title="t", ordinal=3, **publish
    )
    assert uri is not None
    return uri


def tree(ws: Path, ref: str) -> set[str]:
    return set(git(ws, "ls-tree", "-r", "--name-only", ref).split())


@pytest.mark.parametrize("apply", [False, True])
async def test_flowcommit_commits_a_rename_and_a_delete(tmp_path: Path, apply: bool) -> None:
    ws = moved_and_deleted_repo(tmp_path)
    changesets = WorktreeChangesets()
    uri = await kept_with_move_and_delete(ws, tmp_path, changesets, run_id="r1")
    reply = await changesets.commit(uri, params={"apply": apply, "message": "mv"}, principal=None)
    ref = "HEAD" if apply else "flow/r1-3"
    assert tree(ws, ref) == {"README.md", "moved.txt"}, reply
    if apply:
        # Nothing of the move is left uncommitted: the deletion went into the commit too.
        assert git(ws, "status", "--porcelain").strip() == ""
        assert not (ws / "move.txt").exists()
        assert not (ws / "gone.txt").exists()
    else:
        assert tree(ws, "HEAD") == {"README.md", "move.txt", "gone.txt"}


async def test_flowcommit_apply_leaves_the_users_staged_changes_out(tmp_path: Path) -> None:
    ws = moved_and_deleted_repo(tmp_path)
    (ws / "mine.txt").write_text("staged by the user\n", encoding="utf-8")
    (ws / "README.md").write_text("edited by the user\n", encoding="utf-8")
    git(ws, "add", "mine.txt", "README.md")
    changesets = WorktreeChangesets()
    uri = await kept_with_move_and_delete(ws, tmp_path, changesets)
    await changesets.commit(uri, params={"apply": True}, principal=None)
    assert tree(ws, "HEAD") == {"README.md", "moved.txt"}
    assert git(ws, "show", "HEAD:README.md") == "hello"
    status = git(ws, "status", "--porcelain").splitlines()
    assert sorted(status) == ["A  mine.txt", "M  README.md"]


async def test_flowcommit_default_message_falls_back(tmp_path: Path) -> None:
    ws = moved_and_deleted_repo(tmp_path)
    long_line = "x" * 100
    assert commit_subject(None, f"  {long_line}\nsecond line") == "x" * 72
    assert commit_subject("  lbl ", "prompt") == "lbl"
    assert commit_subject(None, "  \n") is None

    changesets = WorktreeChangesets(mask=lambda s: s.replace("hunter2", "***"))
    uri = await kept_with_move_and_delete(
        ws, tmp_path, changesets, run_id="r1", subject="use token hunter2 here"
    )
    await changesets.commit(uri, params={}, principal=None)
    assert git(ws, "log", "-1", "--format=%B", "flow/r1-3").strip() == "use token *** here"

    other = tmp_path / "other"
    other.mkdir()
    ws2 = moved_and_deleted_repo(other)
    uri = await kept_with_move_and_delete(ws2, other, changesets, run_id="r2")
    await changesets.commit(uri, params={}, principal=None)
    assert git(ws2, "log", "-1", "--format=%B", "flow/r2-3").strip() == "flow: run r2 step 3"


async def test_flowcommit_trailer_principal_cannot_forge_lines(tmp_path: Path) -> None:
    ws = moved_and_deleted_repo(tmp_path)
    changesets = WorktreeChangesets(trailer=True)
    uri = await kept_with_move_and_delete(ws, tmp_path, changesets, run_id="r1")
    principal = "alice\r\nSigned-off-by: mallory\x07\x1b[31m"
    await changesets.commit(uri, params={"message": "m"}, principal=principal)
    body = git(ws, "log", "-1", "--format=%B", "flow/r1-3").strip()
    lines = body.splitlines()
    assert lines[0] == "m"
    assert lines[-1].startswith("Flow-Principal: alice")
    assert len(lines) == 3
    assert not any(c in lines[-1] for c in "\r\x07\x1b")
    trailers = git(ws, "log", "-1", "--format=%(trailers:only,unfold)", "flow/r1-3")
    assert [t.split(":", 1)[0] for t in trailers.splitlines() if t] == ["Flow-Principal"]


async def test_flowcommit_concurrent_operations_run_once(tmp_path: Path) -> None:
    """Apply and Commit pressed together: one of them runs, the other finds it resolved."""
    ws = moved_and_deleted_repo(tmp_path)
    changesets = WorktreeChangesets()
    uri = await kept_with_move_and_delete(ws, tmp_path, changesets, run_id="r1")
    results = await asyncio.gather(
        changesets.apply(uri),
        changesets.commit(uri, params={}, principal=None),
        return_exceptions=True,
    )
    failed = [r for r in results if isinstance(r, BaseException)]
    assert len(failed) == 1, results
    assert "no worktree is kept" in str(failed[0])
    applied = (ws / "moved.txt").exists()
    branched = bool(git(ws, "branch", "--list", "flow/r1-3").strip())
    assert applied != branched


async def _cleanup_fails(wt: Any) -> None:
    raise WorktreeError("git worktree remove failed: busy")


@pytest.mark.parametrize("operation", ["apply", "commit"])
async def test_flowcommit_a_failed_cleanup_after_the_side_effect_still_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    # The repo already has the change: the changeset is resolved (Apply is not
    # offered twice) and the worktree left behind is logged, not lost silently.
    ws = moved_and_deleted_repo(tmp_path)
    changesets = WorktreeChangesets()
    uri = await kept_with_move_and_delete(ws, tmp_path, changesets, run_id="r1")
    publisher = changesets._kept[uri].publisher
    monkeypatch.setattr(worktree_module, "discard_worktree", _cleanup_fails)
    with caplog.at_level(logging.WARNING, logger="ahp_host_flow.worktree"):
        if operation == "apply":
            await changesets.apply(uri)
        else:
            await changesets.commit(uri, params={}, principal=None)
    assert cast(Any, publisher).n == 2
    assert str(tmp_path / "wt") in caplog.text
    with pytest.raises(RuntimeError, match="no worktree is kept"):
        await changesets.apply(uri)
    assert changesets._locks == {}


async def test_flowcommit_a_failed_discard_keeps_the_worktree_for_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = moved_and_deleted_repo(tmp_path)
    changesets = WorktreeChangesets()
    uri = await kept_with_move_and_delete(ws, tmp_path, changesets)
    monkeypatch.setattr(worktree_module, "discard_worktree", _cleanup_fails)
    with pytest.raises(WorktreeError, match="busy"):
        await changesets.discard(uri)
    assert uri in changesets._kept
    monkeypatch.undo()
    await changesets.discard(uri)
    assert changesets._kept == {}
    assert changesets._locks == {}


async def test_flowcommit_an_unknown_changeset_leaves_no_lock() -> None:
    changesets = WorktreeChangesets()
    with pytest.raises(RuntimeError, match="no worktree is kept"):
        await changesets.apply("changeset:/nothing")
    assert changesets._locks == {}


@pytest.mark.parametrize("pruned", [False, True])
async def test_flowcommit_discard_after_worktree_deleted(tmp_path: Path, pruned: bool) -> None:
    ws = moved_and_deleted_repo(tmp_path)
    changesets = WorktreeChangesets()
    uri = await kept_with_move_and_delete(ws, tmp_path, changesets)
    shutil.rmtree(tmp_path / "wt")
    if pruned:  # the user also tidied git's own record of it
        git(ws, "worktree", "prune")
    await changesets.discard(uri)
    assert "wt" not in git(ws, "worktree", "list")
    with pytest.raises(RuntimeError, match="no worktree is kept"):
        await changesets.discard(uri)


async def test_flowcommit_commit_failure_leaves_the_index_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = moved_and_deleted_repo(tmp_path)
    changesets = WorktreeChangesets()
    uri = await kept_with_move_and_delete(ws, tmp_path, changesets)
    # No identity anywhere: `git commit` fails after the patch is applied to the index.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-such-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(ws, "config", "--unset", "user.name")
    git(ws, "config", "--unset", "user.email")
    git(ws, "config", "user.useConfigOnly", "true")
    with pytest.raises(WorktreeError, match="git commit failed"):
        await changesets.commit(uri, params={"apply": True}, principal=None)
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=ws, capture_output=True, text=True, check=True
        ).stdout
        == ""
    )
    assert (ws / "move.txt").exists()
    # Still kept: fixing the identity and pressing Commit again works.
    git(ws, "config", "user.name", "flow-test")
    git(ws, "config", "user.email", "flow-test@example.com")
    await changesets.commit(uri, params={"apply": True}, principal=None)
    assert tree(ws, "HEAD") == {"README.md", "moved.txt"}
