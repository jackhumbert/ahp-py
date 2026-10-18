"""Under ahp-node the `flow` agent's changesets carry Apply and Discard (plan M11)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.node import Roots
from ahp_host.node.config import AgentSpec, NodeSettings
from ahp_host.node.runner import create_agents

from ahp_host_flow.worktree import APPLY_OPERATION, DISCARD_OPERATION, worktree_path
from tests.flow_fakes import reset_fakes
from tests.flow_hosting import SESSION, write_toml
from tests.hosting import connect, notifications, open_session, run_turn, shut, state, worker_chats

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not found on PATH"),
]

TOML = """
[providers.fw]
load = "tests.flow_fakes:FileWriterProvider"

[routing]
default = "fw"
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


def script(prompt: str) -> str:
    return f"```js\nreturn await agent({prompt!r}, {{label: 'fw-1', isolation: 'worktree'}})\n```"


def run_id_of(turn: object) -> str:
    (line,) = [n for n in notifications(turn) if n.startswith("Run ")]  # type: ignore[arg-type]
    return line.removeprefix("Run ")


async def test_flow_node_operations_apply_and_discard(tmp_path: Path, node_path: str) -> None:
    ws = tmp_path / "ws"
    init_repo(ws)
    toml = write_toml(tmp_path, node_path, TOML)
    settings = NodeSettings(
        roots=Roots.single(ws),
        agents=(AgentSpec("flow", {"config": str(toml)}),),
        state_dir=tmp_path / "node",
    )
    providers = await create_agents(settings)
    host = Host(providers, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, SESSION, "flow", working_directories=[ws.as_uri()])

        def open_changesets() -> list[str]:
            """Changesets across the worker chats that still offer operations."""
            uris = [
                str(entry["uriTemplate"])
                for worker in worker_chats(host, SESSION)
                for entry in state(host, str(worker["resource"])).get("changesets", [])
            ]
            return [u for u in uris if state(host, u).get("operations")]

        turn = await run_turn(wire, chat, "t1", script("write notes.txt"))
        (uri,) = open_changesets()
        channel: dict[str, Any] = state(host, uri)
        assert [op["id"] for op in channel["operations"]] == [APPLY_OPERATION, DISCARD_OPERATION]
        assert run_id_of(turn)
        reply = await wire.request(
            "invokeChangesetOperation", {"channel": uri, "operationId": APPLY_OPERATION}
        )
        assert "error" not in reply
        assert (ws / "notes.txt").read_text(encoding="utf-8") == "x"

        turn = await run_turn(wire, chat, "t2", script("write other.txt"))
        (second,) = open_changesets()
        reply = await wire.request(
            "invokeChangesetOperation", {"channel": second, "operationId": DISCARD_OPERATION}
        )
        assert "error" not in reply
        assert not (ws / "other.txt").exists()
        assert not worktree_path(tmp_path / "state", run_id_of(turn), 0).exists()
    finally:
        await shut(host, wire, serving)
